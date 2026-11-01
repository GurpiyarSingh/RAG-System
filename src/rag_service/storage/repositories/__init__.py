"""Data-access objects. KB lookups take an :class:`AccessContext` to enforce KB allow-lists.

Repositories never commit. The caller owns the transaction (see
``storage.db.session.transaction``), so several repository calls can be combined into
one atomic unit of work.
"""
