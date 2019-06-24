# Authors: Michal Polovka <mpolovka@redhat.com>
#
# Copyright (C) 2019  Red Hat
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
#

import sys

import pytest
import six
from six import StringIO

from ipalib import api

if six.PY3:
    unicode = str


pytestmark = pytest.mark.needs_ipaapi


@pytest.mark.tier0
class CLITestContext(object):
    """
    Context manager that replaces stdout & stderr, and catches SystemExit

    Whatever was printed to the streams is available in ``stdout`` and
    ``stderr`` attrributes once the with statement finishes.

    When exception is given, asserts that exception is raised. The exception
    will be available in the ``exception`` attribute.
    """
    def __init__(self, exception=None):
        self.exception = exception

    def __enter__(self):
        self.old_streams = sys.stdout, sys.stderr
        self.stdout_fileobj = sys.stdout = StringIO()
        self.stderr_fileobj = sys.stderr = StringIO()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout, sys.stderr = self.old_streams
        self.stdout = self.stdout_fileobj.getvalue()
        self.stderr = self.stderr_fileobj.getvalue()
        self.stdout_fileobj.close()
        self.stderr_fileobj.close()
        if self.exception:
            if not isinstance(exc_value, self.exception):
                return False
            self.exception = exc_value
            return True
        else:
            return None


def test_bz1428690():
    """
    Test for BZ#1428690 - ipa-backup does not create log file at /var/log/
    :return: None
    :raises: AssertionError if the test fails
    """
    with CLITestContext(exception=SystemExit) as ctx:
        api.Backend.cli.run(['backup'])
    assert ctx.exception.code == 1
    assert ctx.stdout == ''
    assert 'not configured' in ctx.stderr
    assert '/var/log' not in ctx.stderr
