from urllib.parse import urlsplit

def normalize_repository_url(value:str)->str:
    """
    validate a Github repository URL and return a consistent form.
    """
    parsed=urlsplit(value.strip())
    
    if parsed.schema.lower()!="https":
        raise ValueError("Use a HTTPS repository URL")
    
    if parsed.netloc.lower()!="github.com":
        raise ValueError("Only github.com repositories are allowed")
    
    if parsed.query or parsed.fragment:
        raise ValueError("Use  repository URL without query parametes or fragments")
    
    path=parsed.path.rstrip("/")
    
    if path.endswith(".git"):
        path=path[:4]
    
    parts=path.strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            "Use a repository URL such as https://github.com/owner/repository."
        )
    owner,name=parts
    
    for part in (owner,name):
        if not all(
            character.isascii()
            and (character.isalnum() or character in "-_.")
            for character in part
        ):
            raise ValueError("The repository URL contains unsupported characters.")
        
        if part in {".", ".."}:
            raise ValueError("Invalid repository path.")

    return f"https://github.com/{owner}/{name}".lower()