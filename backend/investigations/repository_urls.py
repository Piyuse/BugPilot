import re
from urllib.parse import urlsplit


def normalize_repository_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "github.com":
        raise ValueError("Use an HTTPS github.com repository URL.")
    if parsed.query or parsed.fragment:
        raise ValueError("Repository URLs cannot contain query parameters or fragments.")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.strip("/").split("/")
    if len(parts) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in parts):
        raise ValueError("Use a URL such as https://github.com/owner/repository.")
    return f"https://github.com/{parts[0]}/{parts[1]}".lower()


def parse_ticket(value: str, jira_base_url: str = "") -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        configured = urlsplit(jira_base_url)
        if parsed.scheme != "https" or parsed.netloc.lower() != configured.netloc.lower() or not configured.netloc:
            raise ValueError("Use a ticket URL from the configured Jira site, or paste its issue key.")
        match = re.fullmatch(r"/browse/([A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*)/?", parsed.path)
        if not match:
            raise ValueError("Use a Jira browse URL or issue key, for example PAY-142.")
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*", value):
        raise ValueError("Use a Jira issue key such as PAY-142.")
    return value.upper()


def parse_github_issue(value: str, repository_url: str = "") -> dict:
    """Accept browser/API URLs, owner/repo/issues/N, or issues/N with a repo."""
    value = value.strip()
    repository = ""
    number = ""
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or parsed.netloc.lower() not in {"github.com", "api.github.com"}:
            raise ValueError("Use an HTTPS github.com issue URL or api.github.com issue endpoint.")
        if parsed.query:
            raise ValueError("Paste the issue URL without query parameters.")
        pattern = r"/repos/([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?" if parsed.netloc.lower() == "api.github.com" else r"/([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?"
        match = re.fullmatch(pattern, parsed.path)
        if match:
            repository = normalize_repository_url(f"https://github.com/{match[1]}/{match[2]}")
            number = match[3]
    else:
        match = re.fullmatch(r"/?(?:repos/)?([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?", value)
        if match:
            repository = normalize_repository_url(f"https://github.com/{match[1]}/{match[2]}")
            number = match[3]
        else:
            match = re.fullmatch(r"/?(?:issues/|#)?([1-9][0-9]*)/?", value)
            if match:
                if not repository_url:
                    raise ValueError("For issues/123, paste a repository URL or name below. To find the repository automatically, paste a full GitHub issue URL.")
                repository = normalize_repository_url(repository_url)
                number = match[1]
    if not repository or not number or len(number) > 12:
        raise ValueError("Use https://github.com/owner/repo/issues/123, an API issue endpoint, or issues/123 with a repository.")
    slug = repository.removeprefix("https://github.com/")
    if len(slug) > 380:
        raise ValueError("The GitHub repository path is too long.")
    return {
        "issue_key": f"{slug}#{int(number)}",
        "issue_url": f"{repository}/issues/{int(number)}",
        "source_repository_url": repository,
        "source_issue_number": int(number),
    }
