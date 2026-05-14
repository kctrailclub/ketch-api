-- One-time: invalidate all refresh tokens after migration to HttpOnly cookie storage.
-- Run after deploy; every currently-logged-in user re-authenticates once.
DELETE FROM refresh_tokens;
