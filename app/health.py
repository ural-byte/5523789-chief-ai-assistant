import sys

import httpx

from app.config import settings

config = settings()
response = httpx.get(
    config.backend_url + "/internal/health/" + sys.argv[1],
    headers={"Authorization": "Bearer " + config.service_token.get_secret_value()},
    timeout=5,
)
response.raise_for_status()
