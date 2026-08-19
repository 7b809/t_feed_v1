import os

from dotenv import load_dotenv
from upstox_auth import FileStorage, UpstoxAuthenticator

load_dotenv()


async def authenticate_with_totp(
    totp_code: str,
):
    """
    Authenticate against Upstox using current TOTP.

    Returns:
        {
            "status": "success",
            "access_token": "...",
        }
    """

    auth = UpstoxAuthenticator(
        api_key=os.getenv("UPSTOX_API_KEY"),
        api_secret=os.getenv("UPSTOX_API_SECRET"),
        redirect_uri=os.getenv("UPSTOX_REDIRECT_URI"),
        mobile_no=os.getenv("UPSTOX_MOBILE_NO"),
        pin=os.getenv("UPSTOX_PIN"),
        totp_key=int(totp_code),
        storage=FileStorage("tokens/upstox_token.json"),
        headless=True,
        retries=2,
    )

    token = await auth.get_access_token()

    return {
        "status": "success",
        "access_token": token,
    }
