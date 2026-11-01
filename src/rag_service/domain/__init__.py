"""Use-case services that combine repositories, storage and workflows.

Routes stay thin (parse/validate HTTP, call one service method, shape the response),
so the business rules here are testable without HTTP and reusable from scripts.
"""
