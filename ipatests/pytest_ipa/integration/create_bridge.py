import re
import textwrap

from ipatests.pytest_ipa.integration import tasks


def setup_scim_server(host, version="0.1"):
    dir = "/opt/ipa-tuura"
    password = host.config.admin_password
    tasks.install_packages(host, ["unzip", "java-11-openjdk-headless",
                                  "openssl", "maven", "wget", "git",
                                  "firefox", "xorg-x11-server-Xvfb",
                                  "python3-pip"])

    # Download ipa-tuura project
    url = "https://github.com/freeipa/ipa-tuura/archive/refs/tags/{0}.zip".format(version) # noqa: E501
    host.run_command(["wget", url, "-O", "{0}-{1}.zip".format(dir, version)])
    host.run_command(
        ["unzip", "{0}-{1}.zip".format(dir, version), "-d", "/opt/"]
    )

    # Install django requirements
    django_reqs = f"{dir}/src/install/requirements.txt"
    host.run_command(["pip", "install", "-r", f"{django_reqs}"])

    # Prepare models and database
    host.run_command(["python", "manage.py", "makemigrations", "scim"],
                     cwd=f"{dir}/src/ipa-tuura")
    host.run_command(["python", "manage.py", "migrate"],
                     cwd=f"{dir}/src/ipa-tuura")

    # Add necessary admin vars to bashrc
    env_vars = textwrap.dedent(f"""
    export DJANGO_SUPERUSER_PASSWORD={password}
    export DJANGO_SUPERUSER_USERNAME=scim
    export DJANGO_SUPERUSER_EMAIL=scim@{host.domain.name}
    """)

    tasks.backup_file(host, '/etc/bashrc')
    content = host.get_file_contents('/etc/bashrc', encoding='utf-8')
    new_content = content + f"\n{env_vars}"
    host.put_file_contents('/etc/bashrc', new_content)
    host.run_command(['bash'])

    # Create django admin
    host.run_command(["python", "manage.py", "createsuperuser",
                      "--scim_username", "scim", "--noinput"],
                     cwd=f"{dir}/src/ipa-tuura")

    # Open allowed hosts to any for testing
    regex = r"^(ALLOWED_HOSTS) .*$"
    replace = r"\1 = ['*']"
    settings_file = f"{dir}/src/ipa-tuura/root/settings.py"
    settings = host.get_file_contents(settings_file, encoding='utf-8')
    new_settings = re.sub(regex, replace, settings, flags=re.MULTILINE)
    host.put_file_contents(settings_file, new_settings)

    # Setup keycloak service and config files
    contents = textwrap.dedent(f"""
    DJANGO_SUPERUSER_USERNAME=scim
    DJANGO_SUPERUSER_PASSWORD={password}
    DJANGO_SUPERUSER_EMAIL=scim@{host.domain.name}
    """)
    host.put_file_contents("/etc/sysconfig/scim", contents)

    manage = f"{dir}/src/ipa-tuura/manage.py"
    contents = textwrap.dedent(f"""
    [Unit]
    Description=SCIMv2 Bridge Server
    After=network.target

    [Service]
    Type=idle
    WorkingDirectory={dir}/src/ipa-tuura/
    EnvironmentFile=/etc/sysconfig/scim
    # Fix this later
    # User=scim
    # Group=scim
    ExecStart=/usr/bin/python {manage} runserver 0.0.0.0:8000
    TimeoutStartSec=600
    TimeoutStopSec=600

    [Install]
    WantedBy=multi-user.target
    """)
    host.put_file_contents("/etc/systemd/system/scim.service", contents)
    host.run_command(["systemctl", "daemon-reload"])
    host.run_command(["systemctl", "start", "scim"])


def setup_keycloak_scim_plugin(host, bridge_server):
    dir = "/opt/keycloak"
    password = host.config.admin_password

    # Install needed packages
    tasks.install_packages(host, ["unzip", "java-11-openjdk-headless",
                                  "openssl", "maven"])

    # Add necessary admin vars to bashrc
    env_vars = textwrap.dedent(f"""
    export KEYCLOAK_PATH={dir}
    """)

    content = host.get_file_contents('/etc/bashrc', encoding='utf-8')
    new_content = content + f"\n{env_vars}"
    host.put_file_contents('/etc/bashrc', new_content)
    host.run_command(['bash'])

    # Download keycloak plugin
    zipfile = "scim-keycloak-user-storage-spi/archive/refs/tags/0.1.zip"
    url = f"https://github.com/justin-stephenson/{zipfile}"
    dest = "/tmp/keycloak-scim-plugin.zip"
    host.run_command(["wget", "-O", dest, url])

    # Unzip keycloak plugin
    host.run_command(["unzip", dest, "-d", "/tmp"])

    # Install plugin
    host.run_command(["./redeploy-plugin.sh"],
                     cwd="/tmp/scim-keycloak-user-storage-spi-0.1")

    # Fix ownership of plugin files
    host.run_command(["chown", "-R", "keycloak:keycloak", dir])

    # Restore SELinux contexts
    host.run_command(["restorecon", "-R", f"{dir}"])

    # Rerun Keycloak build step and restart to pickup plugin
    # This relies on the KC_* vars set in /etc/bashrc from create_keycloak.py
    host.run_command(['su', '-', 'keycloak', '-c',
                      '/opt/keycloak/bin/kc.sh build'])
    host.run_command(["systemctl", "restart", "keycloak"])
    host.run_command(["/opt/keycloak/bin/kc.sh", "show-config"])

    # Login to keycloak as admin
    kcadmin_sh = "/opt/keycloak/bin/kcadm.sh"
    kcadmin = [kcadmin_sh, "config", "credentials", "--server",
               f"https://{host.hostname}:8443/auth/",
               "--realm", "master", "--user", "admin",
               "--password", password]
    tasks.run_repeatedly(host, kcadmin, timeout=60)

    # Configure SCIM User Storage to point to Bridge server
    provider_type = "org.keycloak.storage.UserStorageProvider"
    host.run_command([kcadmin_sh, "create", "components",
                      "-r", "master",
                      "-s", "name=scimprov",
                      "-s", "providerId=scim",
                      "-s", f"providerType={provider_type}",
                      "-s", "parentId=master",
                      "-s", f'config.scimurl=["{bridge_server}:8000"]',
                      "-s", 'config.loginusername=["scim"]',
                      "-s", f'config.loginpassword=["{password}"]'])


def uninstall_scim_server(host):
    host.run_command(["systemctl", "stop", "scim"], raiseonerr=False)
    host.run_command(["rm", "-rf", "/opt/ipa-tuura",
                      "/etc/sysconfig/scim",
                      "/etc/systemd/system/scim.service",
                      "/tmp/scim-keycloak-user-storage-spi-0.1",
                      "/tmp/keycloak-scim-plugin.zip",
                      "/root/scim.keytab"])
    host.run_command(["systemctl", "daemon-reload"])
    tasks.restore_files(host)


def uninstall_scim_plugin(host):
    host.run_command(["rm", "-rf",
                      "/tmp/scim-keycloak-user-storage-spi-0.1",
                      "/tmp/keycloak-scim-plugin.zip"])
