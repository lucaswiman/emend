"""Exception-handling policy for best-effort analysis paths.

Analysis code often degrades gracefully when the *environment* fails — an
unparseable source file, a corrupt cache database, a missing external tool.
Those handlers must never also swallow exceptions that indicate a bug in
emend itself: a broad ``except Exception`` around orchestration code once
hid a ``NameError`` and silently produced empty analysis results.

Where a broad catch is unavoidable (opaque failure modes from the Rust
extension or CozoDB), guard it::

    except BUG_EXCEPTIONS:
        raise
    except Exception:
        logger.debug("...", exc_info=True)
        ...fallback...

Prefer, in order: shrinking the ``try`` block to the fallible call,
narrowing to concrete exception types, and only then the guarded broad
catch. Silent ``pass`` handlers should log at debug level.
"""

BUG_EXCEPTIONS = (NameError, UnboundLocalError, AssertionError)
