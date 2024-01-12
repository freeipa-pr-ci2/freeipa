#
# Copyright (C) 2024  FreeIPA Contributors see COPYING for license
#

"""
This TencentOS base platform module exports platform related constants.
"""

# Fallback to default path definitions
from __future__ import absolute_import

from ipaplatform.redhat.constants import RedHatConstantsNamespace, User, Group


__all__ = ("constants", "User", "Group")


class TencentOSConstantsNamespace(RedHatConstantsNamespace):
    SECURE_NFS_VAR = None
    NAMED_OPENSSL_ENGINE = "pkcs11"


constants = TencentOSConstantsNamespace()
