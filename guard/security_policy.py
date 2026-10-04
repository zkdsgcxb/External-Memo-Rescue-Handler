"""Shared restrictions for steady-state C++ controllers and their helpers.

Early initramfs activation and authenticated manual rescue are deliberately not
routed through this policy: they have distinct mount/activation responsibilities.
"""

CONTROLLER_RESTRICTIONS = '''UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_DAC_READ_SEARCH CAP_MKNOD CAP_CHOWN
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_NETLINK
SystemCallArchitectures=native
SystemCallFilter=~@mount @module @reboot @swap @raw-io @debug @clock @obsolete @cpu-emulation
SystemCallErrorNumber=EPERM
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictRealtime=yes
MemoryDenyWriteExecute=yes
ProtectSystem=strict
ReadWritePaths=+/run +/dev
'''


def controller_restrictions():
    """No private /dev: device nodes and host-to-service mount updates stay visible."""
    return CONTROLLER_RESTRICTIONS
