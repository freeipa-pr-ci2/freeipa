# Authors:
#   Pavel Zuna <pzuna@redhat.com>
#
# Copyright (C) 2009  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Password migration script
"""
from __future__ import absolute_import

from urllib.parse import parse_qs
import errno
import logging
import os.path

from ipaplatform.paths import paths
from ipapython.dn import DN
from ipapython import ipaldap
from ipalib import errors, create_api

logger = logging.getLogger(os.path.basename(__file__))


def bad_request(start_response):
    """
    Return a 400 Bad Request error.
    """
    status = '400 Bad Request'
    response_headers = []
    response = b''

    start_response(status, response_headers)
    return [response]

def wsgi_redirect(start_response, loc):
    start_response('302 Found', [('Location', loc)])
    return []

def bind(ldap_uri, base_dn, username, password):
    if not base_dn:
        logger.error('migration unable to get base dn')
        raise IOError(errno.EIO, 'Cannot get Base DN')
    bind_dn = DN(('uid', username), ('cn', 'users'), ('cn', 'accounts'), base_dn)
    # ldap_uri should be ldapi:// in all common cases. Enforce start_tls just
    # in case it's a plain LDAP connection.
    start_tls = ldap_uri.startswith('ldap://')
    try:
        conn = ipaldap.LDAPClient(ldap_uri, start_tls=start_tls)
        conn.simple_bind(bind_dn, password)
    except (errors.ACIError, errors.DatabaseError, errors.NotFound) as e:
        logger.error(
            'migration invalid credentials for %s: %s', bind_dn, e)
        raise IOError(
            errno.EPERM, 'Invalid LDAP credentials for user %s' % username)
    except Exception as e:
        logger.error('migration bind failed: %s', e)
        raise IOError(errno.EIO, 'Bind error')
    finally:
        conn.unbind()


def application(environ, start_response):
    if environ.get('REQUEST_METHOD', None) != 'POST':
        return wsgi_redirect(start_response, 'index.html')

    content_type = environ.get('CONTENT_TYPE', '').lower()
    if not content_type.startswith('application/x-www-form-urlencoded'):
        return bad_request(start_response)

    try:
        length = int(environ.get("CONTENT_LENGTH"))
    except (ValueError, TypeError):
        return bad_request(start_response)

    query_string = environ["wsgi.input"].read(length).decode("utf-8")

    try:
        query_dict = parse_qs(query_string)
    except Exception:
        return bad_request(start_response)

    user_query = query_dict.get("username", None)
    if user_query is None or len(user_query) != 1:
        return bad_request(start_response)
    username = user_query[0]

    password_query = query_dict.get("password", None)
    if password_query is None or len(password_query) != 1:
        return bad_request(start_response)
    password = password_query[0]

    status = '200 Success'
    response_headers = []
    result = 'error'
    response = b''

    # API object only for configuration, finalize() not needed
    api = create_api(mode=None)
    api.bootstrap(context='server', confdir=paths.ETC_IPA, in_server=True)
    try:
        bind(api.env.ldap_uri, api.env.basedn, username, password)
    except IOError as err:
        if err.errno == errno.EPERM:
            result = 'invalid-password'
        if err.errno == errno.EIO:
            result = 'migration-error'
    else:
        result = 'ok'
    response_headers.append(('X-IPA-Migrate-Result', result))
    start_response(status, response_headers)
    return [response]
