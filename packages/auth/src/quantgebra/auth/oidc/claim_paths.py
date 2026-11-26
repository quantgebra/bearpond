# ----------------------------------------------------------------------------------------------------------------------
def lookup_claim(claims: dict[str, object], claim_path: str) -> object:
    # the value of a claim of a token; the path is the name of the claim, which may be a long name that has dots in it,
    # such as https://example.com/roles; when there is no claim by that name, it is names of claims inside claims,
    # separated by dots
    value: object = claims.get(claim_path)

    if value is None and "." in claim_path:
        current: object = claims
        for part in claim_path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = None
        value = current

    return value


# ----------------------------------------------------------------------------------------------------------------------
def get_claim_text(claims: dict[str, object], claim_path: str) -> str | None:
    # a claim as text, None when it is not a piece of text that has something in it
    value: object = lookup_claim(claims, claim_path)
    result: str | None = None
    if isinstance(value, str) and value.strip() != "":
        result = value.strip()
    return result


# ----------------------------------------------------------------------------------------------------------------------
def get_claim_list(claims: dict[str, object], claim_path: str) -> list[str]:
    # a claim as a list of names, such as the groups of a person: a list of text, or a single piece of text, which is
    # the one name, and nothing at all for anything else
    value: object = lookup_claim(claims, claim_path)
    names: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                names.append(item)
    elif isinstance(value, str) and value.strip() != "":
        names.append(value.strip())
    return names
