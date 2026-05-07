import re

MAIN_CONTENT_HINT = re.compile(
    r"article|content|post|entry|story|main|markdown|blog|news|body|text",
    re.IGNORECASE,
)

BOILERPLATE_HINT = re.compile(
    r"nav|menu|header|footer|sidebar|breadcrumb|share|social|comment|ads|promo",
    re.IGNORECASE,
)