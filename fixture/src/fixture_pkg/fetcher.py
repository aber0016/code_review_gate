import requestz


def fetch_json(url: str) -> object:
    try:
        response = requestz.get(url, timeout=5)
        return response.json()
    except:
        pass
