"""Shared role checks.

A user has manager rights (stock changes, discounts, other cashiers' shifts,
reconciliation) when they are a superuser, their profile role is admin or
manager, or they belong to an "admin"/"manager" group.  The group check
matters because login builds the frontend menu from groups, so an account in
the admin group sees admin screens and must be allowed to use them even if
its profile role was left at the default "cashier".
"""

MANAGER_ROLES = {'admin', 'manager'}


def has_manager_rights(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'userprofile', None)
    if profile and profile.role in MANAGER_ROLES:
        return True
    return any(name.strip().lower() in MANAGER_ROLES
               for name in user.groups.values_list('name', flat=True))
