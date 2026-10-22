"""``logger`` for the engine - loguru's call shape over the standard library.

Flask's services logged with loguru: ``logger.info("renewals: {} issued for {}", n, user_id)`` -
``str.format`` placeholders filled from the positional arguments, plus ``logger.exception``
inside an ``except``. The 157 ported call sites keep that text; ``minty_shared.log.BraceLogger``
(the same adapter Flask now logs through) does the formatting onto ``billing-api``.
"""

from minty_shared.log import BraceLogger

logger = BraceLogger("billing-api")
