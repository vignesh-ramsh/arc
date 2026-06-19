"""
plugins.authn.hooks.users
=========================
Table-scoped hooks for AuthUser, auto-discovered by relay. AuthUser is NOT
exposed via generic auto-CRUD, so these fire only on authn's own writes — they
are a safety net, not the primary enforcement.

  * normalise username/email
  * NEVER let a raw ``password`` field or a non-argon2 ``pwd_hash`` reach the DB
"""

from __future__ import annotations

from plugins.relay import hook


@hook("AuthUser", ["before_insert", "before_update"])
async def normalise_identity(doc):
    if (u := doc.get("username")):
        doc.set("username", u.strip())
    if (e := doc.get("email")):
        doc.set("email", e.strip().lower())


@hook("AuthUser", "validate")
async def reject_plaintext_password(doc):
    if doc.get("password") is not None:
        doc.fail("Refusing to store a 'password' field — hash it first and set "
                 "pwd_hash.", field="password")
    pwd_hash = doc.get("pwd_hash")
    if doc.is_new and not pwd_hash:
        doc.fail("pwd_hash is required.", field="pwd_hash")
    if pwd_hash is not None and not str(pwd_hash).startswith("$argon2"):
        doc.fail("pwd_hash must be an argon2 hash — never a plaintext password.",
                 field="pwd_hash")
