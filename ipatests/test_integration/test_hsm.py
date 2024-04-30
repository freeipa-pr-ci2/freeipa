#
# Copyright (C) 2023 FreeIPA Contributors see COPYING for license
#

import os.path
import pytest
import random
import re
import string
import time

from ipalib.constants import KRA_TRACKING_REQS
from ipapython.ipaldap import realm_to_serverid
from ipatests.test_integration.base import IntegrationTest
from ipatests.test_integration.test_acme import (
    prepare_acme_client,
    certbot_register,
    certbot_standalone_cert,
    get_selinux_status,
    skip_certbot_tests,
    skip_mod_md_tests,

)
from ipatests.test_integration.test_caless import CALessBase
from ipatests.test_integration.test_cert import get_certmonger_fs_id
from ipatests.test_integration.test_external_ca import (
    install_server_external_ca_step1,
    install_server_external_ca_step2,
    check_CA_flag,
    verify_caentry
)
from ipatests.test_integration.test_ipa_cert_fix import (
    check_status,
    needs_resubmit,
    get_cert_expiry
)
from ipatests.test_integration.test_ipahealthcheck import run_healthcheck
from ipatests.pytest_ipa.integration import tasks
from ipatests.pytest_ipa.integration.env_config import get_global_config
from ipalib import x509 as ipa_x509
from ipaplatform.paths import paths


config = get_global_config()
hsm_lib_path = ''
if config.token_library:
    hsm_lib_path = config.token_library
else:
    hsm_lib_path = '/usr/lib64/pkcs11/libsofthsm2.so'


def get_hsm_token(host):
    """Helper method to get an hsm token
    This method creates a softhsm token if the hsm hardware
    token is not found.
    """
    if host.config.token_name:
        return (host.config.token_name, host.config.token_password)

    token_name = ''.join(
        random.choice(string.ascii_letters) for i in range(10)
    )
    token_passwd = ''.join(
        random.choice(string.ascii_letters) for i in range(10)
    )
    # remove the token if already exist
    host.run_command(
        ['softhsm2-util', '--delete-token', '--token', token_name],
        raiseonerr=False
    )
    host.run_command(
        ['runuser', '-u', 'pkiuser', '--', 'softhsm2-util', '--init-token',
         '--free', '--pin', token_passwd, '--so-pin', token_passwd,
         '--label', token_name]
    )
    return (token_name, token_passwd)


def delete_hsm_token(hosts, token_name):
    for host in hosts:
        if host.config.token_name:
            # assumption: for time being /root/cleantoken.sh is copied
            # host manually. This should be removed in final iteration.
            host.run_command(['sh', '/root/cleantoken.sh'])
        else:
            host.run_command(
                ['softhsm2-util', '--delete-token', '--token', token_name],
                raiseonerr=False
            )


def find_softhsm_token_files(host, token):
    if not host.transport.file_exists(paths.PKI_TOMCAT_ALIAS_DIR):
        return None, []

    result = host.run_command([
        paths.MODUTIL, '-list', 'libsofthsm2',
        '-dbdir', paths.PKI_TOMCAT_ALIAS_DIR
    ])

    serial = None
    state = 'token_name'
    for line in result.stdout_text.split('\n'):
        if state == 'token_name' and 'Token Name:' in line.strip():
            (_label, tokenname) = line.split(':', 1)
            if tokenname.strip() == token:
                state = 'serial'
        elif state == 'serial' and 'Token Serial Number' in line.strip():
            (_label, serial) = line.split(':', 1)
            serial = serial.strip()
            serial = "{}-{}".format(serial[0:4], serial[4:])
            break

    if serial is None:
        raise RuntimeError("can't find softhsm token serial for %s"
                           % token)

    result = host.run_command(
        ['ls', '-l', '/var/lib/softhsm/tokens/'])
    serialdir = None
    for r in result.stdout_text.split('\n'):
        if serial in r:
            dirname = r.split()[-1:][0]
            serialdir = f'/var/lib/softhsm/tokens/{dirname}'
            break
    if serialdir is None:
        raise RuntimeError("can't find softhsm token directory for %s"
                           % serial)
    result = host.run_command(['ls', '-1', serialdir])
    return serialdir, [
        os.path.join(serialdir, file)
        for file in result.stdout_text.strip().split('\n')
    ]


def copy_token_files(src_host, dest_host, token_name):
    """Helper method to copy the token files to replica"""
    # copy the token files to replicas
    if not src_host.config.token_name:
        serialdir, token_files = find_softhsm_token_files(
            src_host, token_name
        )
        if serialdir:
            for host in dest_host:
                tasks.copy_files(src_host, host, token_files)
                host.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])
                host.run_command(
                    ['chown', '-R', 'pkiuser:pkiuser', serialdir]
                )


def check_version(host):
    if tasks.get_pki_version(host) < tasks.parse_version('11.3.0'):
        raise pytest.skip("PKI HSM support is not available")


class TestHSMInstall(IntegrationTest):

    num_replicas = 3
    num_clients = 1
    topology = 'star'

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )
        # copy the token files to replicas
        if (
            cls.master.config.token_library
            and 'nfast' in cls.master.config.token_library
        ):
            for replica in cls.replicas:
                tasks.copy_nfast_data(cls.master, replica)
        copy_token_files(cls.master, cls.replicas, token_name)

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMInstall, cls).uninstall(mh)
        delete_hsm_token([cls.master] + cls.replicas, cls.token_name)

    def test_hsm_install_replica0_ca_less_install(self):
        check_version(self.master)
        tasks.install_replica(
            self.master, self.replicas[0], setup_ca=False,
            setup_dns=True,
            extra_args=('--token-password', self.token_password,)
        )

    def test_hsm_install_replica0_ipa_ca_install(self):
        check_version(self.master)
        tasks.install_ca(
            self.replicas[0],
            extra_args=('--token-password', self.token_password,),
        )

    def test_hsm_install_replica0_ipa_kra_install(self):
        check_version(self.master)
        tasks.install_kra(
            self.replicas[0], first_instance=True,
            extra_args=('--token-password', self.token_password,)
        )

        # Copy the new KRA key material to the other servers.
        if (
            self.master.config.token_library
            and 'nfast' in self.master.config.token_library
        ):
            for dest_host in self.replicas[1], self.replicas[2]:
                tasks.copy_nfast_data(self.replicas[0], dest_host)

        copy_token_files(
            self.replicas[0],
            [self.replicas[1], self.replicas[2], self.master],
            self.token_name
        )

    def test_hsm_install_replica0_ipa_dns_install(self):
        tasks.install_dns(self.replicas[0])

    def test_hsm_install_replica1_with_ca_install(self):
        check_version(self.master)
        tasks.install_replica(
            self.master, self.replicas[1], setup_ca=True,
            extra_args=('--token-password', self.token_password,)
        )

    def test_hsm_install_replica1_ipa_kra_install(self):
        check_version(self.master)
        tasks.install_kra(
            self.replicas[1],
            extra_args=('--token-password', self.token_password,)
        )

    def test_hsm_install_replica1_ipa_dns_install(self):
        check_version(self.master)
        tasks.install_dns(self.replicas[1])

    def test_hsm_install_replica2_with_ca_kra_dns_install(self):
        check_version(self.master)
        tasks.install_replica(
            self.master, self.replicas[2], setup_ca=True, setup_kra=True,
            setup_dns=True,
            extra_args=('--token-password', self.token_password,)
        )

    def test_hsm_install_client(self):
        check_version(self.master)
        tasks.install_client(self.master, self.clients[0])

    def test_hsm_install_issue_user_cert(self):
        check_version(self.master)
        user = 'testuser1'
        csr_file = f'{user}.csr'
        key_file = f'{user}.key'
        cert_file = f'{user}.crt'

        tasks.kinit_admin(self.master)
        tasks.user_add(self.master, user)
        openssl_cmd = [
            'openssl', 'req', '-newkey', 'rsa:2048', '-keyout', key_file,
            '-nodes', '-out', csr_file, '-subj', '/CN=' + user]
        self.master.run_command(openssl_cmd)

        cmd_args = ['ipa', 'cert-request', '--principal', user,
                    '--certificate-out', cert_file, csr_file]
        self.master.run_command(cmd_args)

    def test_hsm_install_healthcheck(self):
        check_version(self.master)
        tasks.install_packages(self.master, ['*ipa-healthcheck'])
        returncode, output = run_healthcheck(
            self.master, output_type="human", failures_only=True
        )
        assert returncode == 0
        assert output == "No issues found."


class TestHSMInstallADTrustBase(IntegrationTest):
    """
    Base test for builtin AD trust installation in combination with other
    components with HSM support
    """
    num_replicas = 1
    master_with_dns = False

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=cls.master_with_dns,
            setup_kra=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )
        # copy token files to replicas
        if (
            cls.master.config.token_library
            and 'nfast' in cls.master.config.token_library
        ):
            for replica in cls.replicas:
                tasks.copy_nfast_data(cls.master, replica)
        copy_token_files(cls.master, cls.replicas, token_name)

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMInstallADTrustBase, cls).uninstall(mh)
        delete_hsm_token([cls.master] + cls.replicas, cls.token_name)

    def test_hsm_adtrust_replica0_all_components(self):
        check_version(self.master)
        tasks.install_replica(
            self.master, self.replicas[0], setup_ca=True,
            setup_adtrust=True, setup_kra=True, setup_dns=True,
            nameservers='master' if self.master_with_dns else None,
            extra_args=('--token-password', self.token_password,)
        )


class TestADTrustInstallWithDNS_KRA_ADTrust(IntegrationTest):

    num_replicas = 1
    master_with_dns = True

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=cls.master_with_dns,
            setup_adtrust=True, setup_kra=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )

        if (
            cls.master.config.token_library
            and 'nfast' in cls.master.config.token_library
        ):
            for replica in cls.replicas:
                tasks.copy_nfast_data(cls.master, replica)
        copy_token_files(cls.master, cls.replicas, token_name)

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestADTrustInstallWithDNS_KRA_ADTrust, cls).uninstall(mh)
        delete_hsm_token([cls.master] + cls.replicas, cls.token_name)

    def test_hsm_adtrust_replica0(self):
        check_version(self.master)
        tasks.install_replica(
            self.master, self.replicas[0], setup_ca=True, setup_kra=True,
            extra_args=('--token-password', self.token_password,)
        )


class TestHSMcertRenewal(IntegrationTest):

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=True, setup_kra=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMcertRenewal, cls).uninstall(mh)
        delete_hsm_token([cls.master], cls.token_name)

    def test_certs_renewal(self):
        """
        Test that the KRA subsystem certificates renew properly
        """
        check_version(self.master)
        CA_TRACKING_REQS = {
            'ocspSigningCert cert-pki-ca': 'caocspSigningCert',
            'subsystemCert cert-pki-ca': 'casubsystemCert',
            'auditSigningCert cert-pki-ca': 'caauditSigningCert'
        }
        CA_TRACKING_REQS.update(KRA_TRACKING_REQS)
        self.master.put_file_contents('/tmp/token_passwd', self.token_password)
        for nickname in CA_TRACKING_REQS:
            cert = tasks.certutil_fetch_cert(
                self.master,
                paths.PKI_TOMCAT_ALIAS_DIR,
                '/tmp/token_passwd',
                nickname,
                token_name=self.token_name,
            )
            starting_serial = int(cert.serial_number)
            cmd_arg = [
                'ipa-getcert', 'resubmit', '-v', '-w',
                '-d', paths.PKI_TOMCAT_ALIAS_DIR,
                '-n', nickname,
            ]
            result = self.master.run_command(cmd_arg)
            request_id = re.findall(r'\d+', result.stdout_text)

            status = tasks.wait_for_request(self.master, request_id[0], 120)
            assert status == "MONITORING"

            args = ['-L', '-h', self.token_name, '-f', '/tmp/token_passwd']
            tasks.run_certutil(self.master, args, paths.PKI_TOMCAT_ALIAS_DIR)

            cert = tasks.certutil_fetch_cert(
                self.master,
                paths.PKI_TOMCAT_ALIAS_DIR,
                '/tmp/token_passwd',
                nickname,
                token_name=self.token_name,
            )
            assert starting_serial != int(cert.serial_number)


class TestHSMCALessToExternalToSelfSignedCA(CALessBase):
    """Test server caless to extarnal CA to self signed scenario"""

    num_replicas = 1

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        super(TestHSMCALessToExternalToSelfSignedCA, cls).install(mh)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMCALessToExternalToSelfSignedCA, cls).uninstall(mh)
        delete_hsm_token([cls.master] + cls.replicas, cls.token_name)

    def test_hsm_caless_server(self):
        """Install CA-less master"""
        check_version(self.master)
        self.create_pkcs12('ca1/server')
        self.prepare_cacert('ca1')

        master = self.install_server(
            extra_args=[
                '--token-name', self.token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', self.token_password
            ]
        )
        assert master.returncode == 0

        # copy the token files to replicas
        if (
            self.master.config.token_library
            and 'nfast' in self.master.config.token_library
        ):
            for replica in self.replicas:
                tasks.copy_nfast_data(self.master, replica)
        copy_token_files(self.master, self.replicas, self.token_name)

    def test_hsm_caless_to_ca_full(self):
        check_version(self.master)
        tasks.install_ca(
            self.master,
            extra_args=('--token-password', self.token_password,),
        )

        ca_show = self.master.run_command(['ipa', 'ca-show', 'ipa'])
        assert 'Subject DN: CN=Certificate Authority,O={}'.format(
            self.master.domain.realm) in ca_show.stdout_text

    def test_hsm_caless_selfsigned_to_external_ca_install(self):
        # Install external CA on master
        result = self.master.run_command([paths.IPA_CACERT_MANAGE, 'renew',
                                         '--external-ca'])
        assert result.returncode == 0

        # Sign CA, transport it to the host and get ipa a root ca paths.
        root_ca_fname, ipa_ca_fname = tasks.sign_ca_and_transport(
            self.master, paths.IPA_CA_CSR, ROOT_CA, IPA_CA)

        # renew CA with externally signed one
        result = self.master.run_command([paths.IPA_CACERT_MANAGE, 'renew',
                                          '--external-cert-file={}'.
                                          format(ipa_ca_fname),
                                          '--external-cert-file={}'.
                                          format(root_ca_fname)])
        assert result.returncode == 0

        # update IPA certificate databases
        result = self.master.run_command([paths.IPA_CERTUPDATE])
        assert result.returncode == 0

        # Check if external CA have "C" flag after the switch
        result = check_CA_flag(self.master)
        assert bool(result), ('External CA does not have "C" flag')

        # Check that ldap entries for the CA have been updated
        remote_cacrt = self.master.get_file_contents(ipa_ca_fname)
        cacrt = ipa_x509.load_pem_x509_certificate(remote_cacrt)
        verify_caentry(self.master, cacrt)

    def test_hsm_caless_external_to_self_signed_ca(self):
        check_version(self.master)
        self.master.run_command([paths.IPA_CACERT_MANAGE, 'renew',
                                 '--self-signed'])
        self.master.run_command([paths.IPA_CERTUPDATE])

    def test_hsm_caless_replica0_with_ca_install(self):
        check_version(self.master)
        tasks.install_replica(
            self.master, self.replicas[0], setup_ca=True,
            extra_args=('--token-password', self.token_password,)
        )


IPA_CA = "ipa_ca.crt"
ROOT_CA = "root_ca.crt"


class TestHSMExternalToSelfSignedCA(IntegrationTest):
    """
    Test of FreeIPA server installation with external CA then
    renew it to self-signed
    """
    num_replicas = 1

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMExternalToSelfSignedCA, cls).uninstall(mh)
        delete_hsm_token([cls.master] + cls.replicas, cls.token_name)

    def test_hsm_external_ca_install(self):
        check_version(self.master)
        # Step 1 of ipa-server-install.
        result = install_server_external_ca_step1(
            self.master,
            extra_args=[
                '--external-ca-type=ms-cs',
                '--token-name', self.token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', self.token_password
            ]
        )
        assert result.returncode == 0

        root_ca_fname, ipa_ca_fname = tasks.sign_ca_and_transport(
            self.master, paths.ROOT_IPA_CSR, ROOT_CA, IPA_CA
        )

        # Step 2 of ipa-server-install.
        result = install_server_external_ca_step2(
            self.master, ipa_ca_fname, root_ca_fname,
            extra_args=[
                '--token-name', self.token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', self.token_password
            ]
        )
        assert result.returncode == 0

        # copy token files to replicas
        if (
            self.master.config.token_library
            and 'nfast' in self.master.config.token_library
        ):
            for replica in self.replicas:
                tasks.copy_nfast_data(self.master, replica)
        copy_token_files(self.master, self.replicas, self.token_name)

    def test_hsm_external_kra_install(self):
        check_version(self.master)
        tasks.install_kra(
            self.master, first_instance=True,
            extra_args=('--token-password', self.token_password,)
        )

        # Copy the new KRA key material to the other server(s).
        if (
            self.master.config.token_library
            and 'nfast' in self.master.config.token_library
        ):
            for replica in self.replicas:
                tasks.copy_nfast_data(self.master, replica)
        copy_token_files(self.master, self.replicas, self.token_name)

    def test_hsm_external_to_self_signed_ca(self):
        check_version(self.master)
        self.master.run_command([paths.IPA_CACERT_MANAGE, 'renew',
                                 '--self-signed'])
        self.master.run_command([paths.IPA_CERTUPDATE])

    def test_hsm_external_ca_replica0_install(self):
        check_version(self.master)
        tasks.install_replica(
            self.master, self.replicas[0], setup_kra=True,
            extra_args=('--token-password', self.token_password,)
        )


@pytest.fixture
def expire_cert_critical():
    """
    Fixture to expire the certs by moving the system date using
    date -s command and revert it back
    """

    hosts = dict()

    def _expire_cert_critical(host):
        hosts['host'] = host
        # move date to expire certs
        tasks.move_date(host, 'stop', '+3Years+1day')

    yield _expire_cert_critical

    host = hosts.pop('host')
    # Prior to uninstall remove all the cert tracking to prevent
    # errors from certmonger trying to check the status of certs
    # that don't matter because we are uninstalling.
    host.run_command(['systemctl', 'stop', 'certmonger'])
    # Important: run_command with a str argument is able to
    # perform shell expansion but run_command with a list of
    # arguments is not
    host.run_command('rm -fv ' + paths.CERTMONGER_REQUESTS_DIR + '*')
    tasks.uninstall_master(host)
    tasks.move_date(host, 'start', '-3Years-1day')


class TestHSMcertFix(IntegrationTest):

    master_with_dns = False

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=cls.master_with_dns,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd,
                '--no-ntp'
            )
        )

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMcertFix, cls).uninstall(mh)
        delete_hsm_token([cls.master], cls.token_name)

    def test_hsm_renew_expired_cert_on_master(self, expire_cert_critical):
        check_version(self.master)
        expire_cert_critical(self.master)

        # wait for cert expiry
        check_status(self.master, 8, "CA_UNREACHABLE")

        self.master.run_command(['ipa-cert-fix', '-v'], stdin_text='yes\n')

        check_status(self.master, 9, "MONITORING", timeout=1000)

        # second iteration of ipa-cert-fix
        result = self.master.run_command(
            ['ipa-cert-fix', '-v'],
            stdin_text='yes\n'
        )
        assert "Nothing to do" in result.stdout_text
        check_status(self.master, 9, "MONITORING")


class TestHSMcertFixKRA(IntegrationTest):

    master_with_dns = False

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=cls.master_with_dns,
            setup_kra=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd,
                '--no-ntp'
            )
        )

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMcertFixKRA, cls).uninstall(mh)
        delete_hsm_token([cls.master], cls.token_name)

    def test_hsm_renew_expired_cert_with_kra(self, expire_cert_critical):
        check_version(self.master)
        expire_cert_critical(self.master)

        # check if all subsystem cert expired
        check_status(self.master, 11, "CA_UNREACHABLE")

        self.master.run_command(['ipa-cert-fix', '-v'], stdin_text='yes\n')

        check_status(self.master, 12, "MONITORING")


class TestHSMcertFixReplica(IntegrationTest):

    num_replicas = 1
    master_with_dns = False

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=cls.master_with_dns,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd,
                '--no-ntp'
            )
        )
        # copy the token files to replicas
        if (
            cls.master.config.token_library
            and 'nfast' in cls.master.config.token_library
        ):
            for replica in cls.replicas:
                tasks.copy_nfast_data(cls.master, replica)
        copy_token_files(cls.master, cls.replicas, token_name)

        tasks.install_replica(
            cls.master, cls.replicas[0], setup_ca=True,
            nameservers='master' if cls.master_with_dns else None,
            extra_args=('--token-password', token_passwd,)
        )

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMcertFixReplica, cls).uninstall(mh)
        delete_hsm_token([cls.master] + cls.replicas, cls.token_name)

    @pytest.fixture
    def expire_certs(self):
        # move system date to expire certs
        for host in self.master, self.replicas[0]:
            tasks.move_date(host, 'stop', '+3years+1days')
            host.run_command(
                ['ipactl', 'restart', '--ignore-service-failures']
            )

        yield

        # move date back on replica and master
        for host in self.replicas[0], self.master:
            tasks.uninstall_master(host)
            tasks.move_date(host, 'start', '-3years-1days')

    def test_hsm_renew_expired_cert_replica(self, expire_certs):
        check_version(self.master)
        # wait for cert expiry
        check_status(self.master, 8, "CA_UNREACHABLE")

        self.master.run_command(['ipa-cert-fix', '-v'], stdin_text='yes\n')

        check_status(self.master, 9, "MONITORING")

        # replica operations
        # 'Server-Cert cert-pki-ca' cert will be in CA_UNREACHABLE state
        cmd = self.replicas[0].run_command(
            ['getcert', 'list',
             '-d', paths.PKI_TOMCAT_ALIAS_DIR,
             '-n', 'Server-Cert cert-pki-ca']
        )
        req_id = get_certmonger_fs_id(cmd.stdout_text)
        tasks.wait_for_certmonger_status(
            self.replicas[0], ('CA_UNREACHABLE'), req_id, timeout=600
        )
        # get initial expiry date to compare later with renewed cert
        initial_expiry = get_cert_expiry(
            self.replicas[0],
            paths.PKI_TOMCAT_ALIAS_DIR,
            'Server-Cert cert-pki-ca'
        )

        # check that HTTP,LDAP,PKINIT are renewed and in MONITORING state
        instance = realm_to_serverid(self.master.domain.realm)
        dirsrv_cert = paths.ETC_DIRSRV_SLAPD_INSTANCE_TEMPLATE % instance
        for cert in (paths.KDC_CERT, paths.HTTPD_CERT_FILE):
            cmd = self.replicas[0].run_command(
                ['getcert', 'list', '-f', cert]
            )
            req_id = get_certmonger_fs_id(cmd.stdout_text)
            tasks.wait_for_certmonger_status(
                self.replicas[0], ('MONITORING'), req_id, timeout=600
            )

        cmd = self.replicas[0].run_command(
            ['getcert', 'list', '-d', dirsrv_cert]
        )
        req_id = get_certmonger_fs_id(cmd.stdout_text)
        tasks.wait_for_certmonger_status(
            self.replicas[0], ('MONITORING'), req_id, timeout=600
        )

        # check if replication working fine
        testuser = 'testuser1'
        password = 'Secret@123'
        stdin = (f"{self.master.config.admin_password}\n"
                 f"{self.master.config.admin_password}\n"
                 f"{self.master.config.admin_password}\n")
        self.master.run_command(['kinit', 'admin'], stdin_text=stdin)
        tasks.user_add(self.master, testuser, password=password)
        self.replicas[0].run_command(['kinit', 'admin'], stdin_text=stdin)
        self.replicas[0].run_command(['ipa', 'user-show', testuser])

        # renew shared certificates by resubmitting to certmonger
        cmd = self.replicas[0].run_command(
            ['getcert', 'list', '-f', paths.RA_AGENT_PEM]
        )
        req_id = get_certmonger_fs_id(cmd.stdout_text)
        if needs_resubmit(self.replicas[0], req_id):
            self.replicas[0].run_command(
                ['getcert', 'resubmit', '-i', req_id]
            )
            tasks.wait_for_certmonger_status(
                self.replicas[0], ('MONITORING'), req_id, timeout=600
            )
        for cert_nick in ('auditSigningCert cert-pki-ca',
                          'ocspSigningCert cert-pki-ca',
                          'subsystemCert cert-pki-ca'):
            cmd = self.replicas[0].run_command(
                ['getcert', 'list',
                 '-d', paths.PKI_TOMCAT_ALIAS_DIR,
                 '-n', cert_nick]
            )
            req_id = get_certmonger_fs_id(cmd.stdout_text)
            if needs_resubmit(self.replicas[0], req_id):
                self.replicas[0].run_command(
                    ['getcert', 'resubmit', '-i', req_id]
                )
                tasks.wait_for_certmonger_status(
                    self.replicas[0], ('MONITORING'), req_id, timeout=600
                )

        self.replicas[0].run_command(
            ['ipa-cert-fix', '-v'], stdin_text='yes\n'
        )

        check_status(self.replicas[0], 9, "MONITORING")

        # Sometimes certmonger takes time to update the cert status
        # So check in nssdb instead of relying on getcert command
        renewed_expiry = get_cert_expiry(
            self.replicas[0],
            paths.PKI_TOMCAT_ALIAS_DIR,
            'Server-Cert cert-pki-ca'
        )
        assert renewed_expiry > initial_expiry


class TestHSMNegative(IntegrationTest):

    master_with_dns = False

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name

    def test_hsm_negative_wrong_token_details(self):
        check_version(self.master)
        # wrong token name
        result = tasks.install_master(
            self.master, raiseonerr=False,
            extra_args=(
                '--token-name', 'random_token',
                '--token-library-path', hsm_lib_path,
                '--token-password', self.token_password
            )
        )
        # assert 'error message non existing token name' in result.stderr_text
        assert result.returncode != 0

        # wrong token password
        result = tasks.install_master(
            self.master, raiseonerr=False,
            extra_args=(
                '--token-name', self.token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', 'token_passwd'
            )
        )
        # assert 'error message wrong  passwd' in result.stderr_text
        assert result.returncode != 0

        # wrong token lib
        result = tasks.install_master(
            self.master, raiseonerr=False,
            extra_args=(
                '--token-name', self.token_name,
                '--token-library-path', '/tmp/non_existing_hsm_lib_path',
                '--token-password', self.token_password
            )
        )
        # assert 'error message non existing token lib' in result.stderr_text
        assert result.returncode != 0

    def test_hsm_negative_special_char_token_name(self):
        check_version(self.master)
        token_name = 'hsm token'
        token_passwd = 'Secret123'
        self.master.run_command(
            ['softhsm2-util', '--delete-token', '--token', token_name],
            raiseonerr=False
        )
        self.master.run_command(
            ['runuser', '-u', 'pkiuser', '--', 'softhsm2-util', '--init-token',
             '--free', '--pin', token_passwd, '--so-pin', token_passwd,
             '--label', token_name]
        )

        # special character in token name
        result = tasks.install_master(
            self.master, raiseonerr=False,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )
        # assert 'error message non existing token lib' in result.stderr_text
        assert result.returncode != 0


class TestHSMACME(CALessBase):

    num_clients = 1

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        super(TestHSMACME, cls).install(mh)

        # install packages before client install in case of IPA DNS problems
        cls.acme_server = prepare_acme_client(cls.master, cls.clients[0])

        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )

        tasks.install_client(cls.master, cls.clients[0])

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMACME, cls).uninstall(mh)
        delete_hsm_token([cls.master], cls.token_name)

    @pytest.mark.skipif(skip_certbot_tests, reason='certbot not available')
    def test_certbot_certonly_standalone(self):
        check_version(self.master)
        # enable ACME on server
        tasks.kinit_admin(self.master)
        self.master.run_command(['ipa-acme-manage', 'enable'])
        # register account to certbot
        certbot_register(self.clients[0], self.acme_server)
        # request ACME cert with certbot
        certbot_standalone_cert(self.clients[0], self.acme_server)

    @pytest.mark.skipif(skip_mod_md_tests, reason='mod_md not available')
    def test_mod_md(self):
        check_version(self.master)
        if get_selinux_status(self.clients[0]):
            # mod_md requires its own SELinux policy to grant perms to
            # maintaining ACME registration and cert state.
            raise pytest.skip("SELinux is enabled, this will fail")
        # write config
        self.clients[0].run_command(['mkdir', '-p', '/etc/httpd/conf.d'])
        self.clients[0].run_command(['mkdir', '-p', '/etc/httpd/md'])
        self.clients[0].put_file_contents(
            '/etc/httpd/conf.d/md.conf',
            '\n'.join([
                f'MDCertificateAuthority {self.acme_server}',
                'MDCertificateAgreement accepted',
                'MDStoreDir  /etc/httpd/md',
                f'MDomain {self.clients[0].hostname}',
                '<VirtualHost *:443>',
                f'    ServerName {self.clients[0].hostname}',
                '    SSLEngine on',
                '</VirtualHost>\n',
            ]),
        )

        # To check for successful cert issuance means knowing how mod_md
        # stores certificates, or looking for specific log messages.
        # If the thing we are inspecting changes, the test will break.
        # So I prefer a conservative sleep.
        #
        self.clients[0].run_command(['systemctl', 'restart', 'httpd'])
        time.sleep(15)

        # We expect mod_md has acquired the certificate by now.
        # Perform a graceful restart to begin using the cert.
        # (If mod_md ever learns to start using newly acquired
        # certificates /without/ the second restart, then both
        # of these sleeps can be replaced by "loop until good".)
        #
        self.clients[0].run_command(['systemctl', 'reload', 'httpd'])
        time.sleep(3)

        # HTTPS request from server to client (should succeed)
        self.master.run_command(
            ['curl', f'https://{self.clients[0].hostname}'])

        # clean-up
        self.clients[0].run_command(['rm', '-rf', '/etc/httpd/md'])
        self.clients[0].run_command(['rm', '-f', '/etc/httpd/conf.d/md.conf'])


class TestHSMBackupRestore(IntegrationTest):

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMBackupRestore, cls).uninstall(mh)
        delete_hsm_token([cls.master], cls.token_name)

    def test_hsm_backup_restore(self):
        check_version(self.master)
        backup_path = tasks.get_backup_dir(self.master)

        self.master.run_command(['ipa-server-install',
                                 '--uninstall',
                                 '-U'])
        assert not self.master.transport.file_exists(
            paths.IPA_CUSTODIA_KEYS)
        assert not self.master.transport.file_exists(
            paths.IPA_CUSTODIA_CONF)

        self.master.run_command(
            ['ipa-restore', backup_path],
            stdin_text=f'{self.master.config.dirman_password}\nyes'
        )


@pytest.fixture
def issue_and_expire_acme_cert():
    """Fixture to expire cert by moving date past expiry of acme cert"""
    hosts = []

    def _issue_and_expire_acme_cert(
        master, client,
        acme_server_url, no_of_cert=1
    ):

        hosts.append(master)
        hosts.append(client)

        # enable the ACME service on master
        master.run_command(['ipa-acme-manage', 'enable'])

        # register the account with certbot
        certbot_register(client, acme_server_url)

        # request a standalone acme cert
        certbot_standalone_cert(client, acme_server_url, no_of_cert)

        # move system date to expire acme cert
        for host in hosts:
            tasks.kdestroy_all(host)
            tasks.move_date(host, 'stop', '+90days+2hours')

        # restart ipa services as date moved and wait to get things settle
        time.sleep(10)
        master.run_command(['ipactl', 'restart'])
        time.sleep(10)

        tasks.get_kdcinfo(master)
        # Note raiseonerr=False:
        # the assert is located after kdcinfo retrieval.
        # run kinit command repeatedly until sssd gets settle
        # after date change
        tasks.run_repeatedly(
            master, "KRB5_TRACE=/dev/stdout kinit admin",
            stdin_text='{0}\n{0}\n{0}\n'.format(
                master.config.admin_password
            )
        )
        # Retrieve kdc.$REALM after the password change, just in case SSSD
        # domain status flipped to online during the password change.
        tasks.get_kdcinfo(master)

    yield _issue_and_expire_acme_cert

    # move back date
    for host in hosts:
        tasks.move_date(host, 'start', '-90days-2hours')

    # restart ipa services as date moved and wait to get things settle
    # if the internal fixture was not called (for instance because the test
    # was skipped), hosts = [] and hosts[0] would produce an IndexError
    # exception.
    if hosts:
        time.sleep(10)
        hosts[0].run_command(['ipactl', 'restart'])
        time.sleep(10)


class TestHSMACMEPrune(IntegrationTest):
    """Validate that ipa-acme-manage configures dogtag for pruning"""

    num_clients = 1

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        super(TestHSMACMEPrune, cls).install(mh)

        # install packages before client install in case of IPA DNS problems
        cls.acme_server = prepare_acme_client(cls.master, cls.clients[0])

        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=True,
            random_serial=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )
        tasks.install_client(cls.master, cls.clients[0])

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMACMEPrune, cls).uninstall(mh)
        delete_hsm_token([cls.master], cls.token_name)

    def test_hsm_prune_cert_manual(self, issue_and_expire_acme_cert):
        """Test to prune expired certificate by manual run"""
        if (tasks.get_pki_version(self.master)
           < tasks.parse_version('11.3.0')):
            raise pytest.skip("Certificate pruning is not available")

        issue_and_expire_acme_cert(
            self.master, self.clients[0], self.acme_server)

        # check that the certificate issued for the client
        result = self.master.run_command(
            ['ipa', 'cert-find', '--subject', self.clients[0].hostname]
        )
        assert f'CN={self.clients[0].hostname}' in result.stdout_text

        # run prune command manually
        self.master.run_command(['ipa-acme-manage', 'pruning', '--enable'])
        self.master.run_command(['ipactl', 'restart'])
        self.master.run_command(['ipa-acme-manage', 'pruning', '--run'])
        # wait for cert to get prune
        time.sleep(50)

        # check if client cert is removed
        result = self.master.run_command(
            ['ipa', 'cert-find', '--subject', self.clients[0].hostname],
            raiseonerr=False
        )
        assert f'CN={self.clients[0].hostname}' not in result.stdout_text

    def test_hsm_prune_cert_cron(self, issue_and_expire_acme_cert):
        """Test to prune expired certificate by cron job"""
        if (tasks.get_pki_version(self.master)
           < tasks.parse_version('11.3.0')):
            raise pytest.skip("Certificate pruning is not available")

        issue_and_expire_acme_cert(
            self.master, self.clients[0], self.acme_server)

        # check that the certificate issued for the client
        result = self.master.run_command(
            ['ipa', 'cert-find', '--subject', self.clients[0].hostname]
        )
        assert f'CN={self.clients[0].hostname}' in result.stdout_text

        # enable pruning
        self.master.run_command(['ipa-acme-manage', 'pruning', '--enable'])

        # cron would be set to run the next minute
        cron_minute = self.master.run_command(
            [
                "python3",
                "-c",
                (
                    "from datetime import datetime, timedelta; "
                    "print(int((datetime.now() + "
                    "timedelta(minutes=5)).strftime('%M')))"
                ),
            ]
        ).stdout_text.strip()
        self.master.run_command(
            ['ipa-acme-manage', 'pruning',
             f'--cron={cron_minute} * * * *']
        )
        self.master.run_command(['ipactl', 'restart'])
        # wait for 5 minutes to cron to execute and 20 sec for just in case
        time.sleep(320)

        # check if client cert is removed
        result = self.master.run_command(
            ['ipa', 'cert-find', '--subject', self.clients[0].hostname],
            raiseonerr=False
        )
        assert f'CN={self.clients[0].hostname}' not in result.stdout_text


class TestHSMVault(IntegrationTest):
    """Validate that vault works properly"""

    num_clients = 1

    @classmethod
    def install(cls, mh):
        check_version(cls.master)
        super(TestHSMVault, cls).install(mh)

        # Enable pkiuser to read softhsm tokens
        cls.master.run_command(['usermod', 'pkiuser', '-a', '-G', 'ods'])

        token_name, token_passwd = get_hsm_token(cls.master)
        cls.token_password = token_passwd
        cls.token_name = token_name
        tasks.install_master(
            cls.master, setup_dns=True,
            setup_kra=True,
            extra_args=(
                '--token-name', token_name,
                '--token-library-path', hsm_lib_path,
                '--token-password', token_passwd
            )
        )
        tasks.install_client(cls.master, cls.clients[0])

    @classmethod
    def uninstall(cls, mh):
        check_version(cls.master)
        super(TestHSMVault, cls).uninstall(mh)

    def test_hsm_vault_create_and_retrieve_master(self):
        vault_name = "testvault"
        vault_password = "password"
        vault_data = "SSBsb3ZlIENJIHRlc3RzCg=="

        # create vault on master
        tasks.kinit_admin(self.master)

        self.master.run_command([
            "ipa", "vault-add", vault_name,
            "--password", vault_password,
            "--type", "symmetric",
        ])

        # archive vault
        self.master.run_command([
            "ipa", "vault-archive", vault_name,
            "--password", vault_password,
            "--data", vault_data,
        ])

        # wait after archival
        time.sleep(45)

        # retrieve vault on master
        self.master.run_command([
            "ipa", "vault-retrieve",
            vault_name,
            "--password", vault_password,
        ])

        # retrieve on client
        tasks.kinit_admin(self.clients[0])
        self.clients[0].run_command([
            "ipa", "vault-retrieve",
            vault_name,
            "--password", vault_password,
        ])
