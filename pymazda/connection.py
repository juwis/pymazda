import aiohttp
import base64
import hashlib
import json
import logging
import time
from urllib.parse import urlencode

from pymazda.crypto_utils import encryptAES128CBCBufferToBase64String, decryptAES128CBCBufferToString, encryptRSAECBPKCS1Padding
from pymazda.exceptions import MazdaException, MazdaAPIEncryptionException, MazdaAuthenticationException, MazdaAccountLockedException, MazdaTokenExpiredException


APP_CODE = "202007270941270111799"
BASE_URL = "https://0cxo7m58.mazda.com/prod/"
USHER_URL = "https://ptznwbh8.mazda.com/appapi/v1/"
IV = "0102030405060708"
SIGNATURE_MD5 = "C383D8C4D279B78130AD52DC71D95CAA"
APP_PACKAGE_ID = "com.interrait.mymazda"
DEVICE_ID = "D9E89AFC-BD3C-309F-A48C-A2A9466DFE9C"
USER_AGENT_BASE_API = "MyMazda-Android/7.1.0"
USER_AGENT_USHER_API = "MyMazda/7.1.0 (Google Pixel 3a; Android 11)";
APP_OS = "Android"
APP_VERSION = "7.1.0"

MAX_RETRIES = 4

class Connection:
    """Main class for handling MyMazda API connection"""
    
    def __init__(self, email, password, websession=None):
        self.email = email
        self.password = password

        self.enc_key = None
        self.sign_key = None

        self.access_token = None
        self.access_token_expiration_ts = None

        if websession is None:
            self._session = aiohttp.ClientSession()
        else:
            self._session = websession
        
        self.logger = logging.getLogger(__name__)

    def __get_timestamp_str_ms(self):
        return str(int(round(time.time() * 1000)))

    def __get_timestamp_str(self):
        return str(int(round(time.time())))

    def __get_decryption_key_from_app_code(self):
        val1 = hashlib.md5((APP_CODE + APP_PACKAGE_ID).encode()).hexdigest().upper()
        val2 = hashlib.md5((val1 + SIGNATURE_MD5).encode()).hexdigest().lower()
        return val2[4:20]

    def __get_temporary_sign_key_from_app_code(self, appCode):
        val1 = hashlib.md5((appCode + APP_PACKAGE_ID).encode()).hexdigest().upper()
        val2 = hashlib.md5((val1 + SIGNATURE_MD5).encode()).hexdigest().lower()
        return val2[20:32] + val2[0:10] + val2[4:6]

    def __get_sign_from_timestamp(self, timestamp):
        if timestamp is None or timestamp == "":
            return ""

        timestamp_extended = (timestamp + timestamp[6:] + timestamp[3:]).upper()

        temporary_sign_key = self.__get_temporary_sign_key_from_app_code(APP_CODE)

        return self.__get_payload_sign(timestamp_extended, temporary_sign_key).upper()

    def __get_sign_from_payload_and_timestamp(self, payload, timestamp):
        if timestamp is None or timestamp == "":
            return ""
        if self.sign_key is None or self.sign_key == "":
            raise MazdaException("Missing sign key")

        return self.__get_payload_sign(self.__encrypt_payload_using_key(payload) + timestamp + timestamp[6:] + timestamp[3:], self.sign_key)

    def __get_payload_sign(self, encryptedPayloadAndTimestamp, signKey):
        return hashlib.sha256((encryptedPayloadAndTimestamp + signKey).encode()).hexdigest().upper()

    def __encrypt_payload_using_key(self, payload):
        if self.enc_key is None or self.enc_key == "":
            raise MazdaException("Missing encryption key")
        if payload is None or payload == "":
            return ""

        return encryptAES128CBCBufferToBase64String(payload.encode("utf-8"), self.enc_key, IV)

    def __decrypt_payload_using_app_code(self, payload):
        buf = base64.b64decode(payload)
        key = self.__get_decryption_key_from_app_code()
        decrypted = decryptAES128CBCBufferToString(buf, key, IV)
        return json.loads(decrypted)

    def __decrypt_payload_using_key(self, payload):
        if self.enc_key is None or self.enc_key == "":
            raise MazdaException("Missing encryption key")

        buf = base64.b64decode(payload)
        decrypted = decryptAES128CBCBufferToString(buf, self.enc_key, IV)
        return json.loads(decrypted)

    def __encrypt_payload_with_public_key(self, password, publicKey):
        timestamp = self.__get_timestamp_str()
        encryptedBuffer = encryptRSAECBPKCS1Padding(password + ":" + timestamp, publicKey)
        return base64.b64encode(encryptedBuffer).decode("utf-8")

    async def api_request(self, method, uri, query_dict={}, body_dict={}, needs_keys=True, needs_auth=False):
        return await self.__api_request_retry(method, uri, query_dict, body_dict, needs_keys, needs_auth, num_retries=0)
    
    async def __api_request_retry(self, method, uri, query_dict={}, body_dict={}, needs_keys=True, needs_auth=False, num_retries=0):
        if num_retries > MAX_RETRIES:
            raise MazdaException("Request exceeded max number of retries")

        if needs_keys:
            await self.__ensure_keys_present()
        if needs_auth:
            await self.__ensure_token_is_valid()

        retry_message = (" attempt #" + (num_retries + 1)) if (num_retries > 0) else ""
        self.logger.debug(f"Sending {method} request to {uri}{retry_message}")

        try:
            return await self.__send_api_request(method, uri, query_dict, body_dict, needs_keys, needs_auth)
        except (MazdaAPIEncryptionException):
            self.logger.debug("Server reports request was not encrypted properly. Retrieving new encryption keys.")
            await self.__retrieve_keys()
            return await self.__api_request_retry(method, uri, query_dict, body_dict, needs_keys, needs_auth, num_retries + 1)
        except (MazdaTokenExpiredException):
            self.logger.debug("Server reports access token was expired. Retrieving new access token.")
            await self.login()
            return await self.__api_request_retry(method, uri, query_dict, body_dict, needs_keys, needs_auth, num_retries + 1)

    async def __send_api_request(self, method, uri, query_dict={}, body_dict={}, needs_keys=True, needs_auth=False):
        timestamp = self.__get_timestamp_str_ms()

        original_query_str = ""
        encrypted_query_dict = {}

        if query_dict:
            original_query_str = urlencode(query_dict)
            encrypted_query_dict["params"] = self.__encrypt_payload_using_key(original_query_str)

        original_body_str = ""
        encrypted_body_Str = ""
        if body_dict:
            original_body_str = json.dumps(body_dict)
            encrypted_body_Str = self.__encrypt_payload_using_key(original_body_str)

        headers = {
            "device-id": DEVICE_ID,
            "app-code": APP_CODE,
            "app-os": APP_OS,
            "user-agent": USER_AGENT_BASE_API,
            "app-version": APP_VERSION,
            "app-unique-id": APP_PACKAGE_ID,
            "region": "us",
            "access-token": (self.access_token if needs_auth else ""),
            "language": "en-US",
            "locale": "en-US",
            "X-acf-sensor-data": "",
            "req-id": "req_" + timestamp,
            "timestamp": timestamp
        }

        if "checkVersion" in uri:
            headers["sign"] = self.__get_sign_from_timestamp(timestamp)
        elif method == "GET":
            headers["sign"] = self.__get_sign_from_payload_and_timestamp(original_query_str, timestamp)
        elif method == "POST":
            headers["sign"] = self.__get_sign_from_payload_and_timestamp(original_body_str, timestamp)

        response = await self._session.request(method, BASE_URL + uri, headers=headers, data=encrypted_body_Str)

        response_json = await response.json()

        if response_json["state"] == "S":
            if "checkVersion" in uri:
                return self.__decrypt_payload_using_app_code(response_json["payload"])
            else:
                return self.__decrypt_payload_using_key(response_json["payload"])
        elif response_json["errorCode"] == 600001:
            raise MazdaAPIEncryptionException("Server rejected encrypted request")
        elif response_json["errorCode"] == 600002:
            raise MazdaTokenExpiredException("Token expired")
        else:
            raise MazdaException("Request failed for an unknown reason")

    async def __ensure_keys_present(self):
        if self.enc_key is None or self.sign_key is None:
            await self.__retrieve_keys()

    async def __ensure_token_is_valid(self):
        if self.access_token is None or self.access_token_expiration_ts is None or self.access_token_expiration_ts <= time.time():
            await self.login()

    async def __retrieve_keys(self):
        self.logger.debug("Retrieving encryption keys")
        response = await self.api_request("POST", "service/checkVersion", needs_keys=False, needs_auth=False)
        self.logger.debug("Successfully retrieved encryption keys")

        self.enc_key = response["encKey"]
        self.sign_key = response["signKey"]

    async def login(self):
        self.logger.debug("Logging in as " + self.email)
        self.logger.debug("Retrieving public key to encrypt password")
        encryption_key_response = await self._session.request(
            "GET",
            USHER_URL + "system/encryptionKey",
            params={
                "appId": "MazdaApp",
                "locale": "en-US",
                "deviceId": "ACCT1195961580",
                "sdkVersion": "11.2.0000.002"
            },
            headers={
                "User-Agent": USER_AGENT_USHER_API
            }
        )
        
        encryption_key_response_json = await encryption_key_response.json()

        public_key = encryption_key_response_json["data"]["publicKey"]
        encrypted_password = self.__encrypt_payload_with_public_key(self.password, public_key)
        version_prefix = encryption_key_response_json["data"]["versionPrefix"]

        self.logger.debug("Sending login request")
        login_response = await self._session.request(
            "POST",
            USHER_URL + "user/login",
            headers={
                "User-Agent": USER_AGENT_USHER_API
            },
            json={
                "appId": "MazdaApp",
                "deviceId": "ACCT1195961580",
                "locale": "en-US",
                "password": version_prefix + encrypted_password,
                "sdkVersion": "11.2.0000.002",
                "userId": self.email,
                "userIdType": "email"
            })

        login_response_json = await login_response.json()

        if login_response_json["status"] == "INVALID_CREDENTIAL":
            raise MazdaAuthenticationException("Invalid email or password")
        if login_response_json["status"] == "USER_LOCKED":
            raise MazdaAccountLockedException("Account has been locked")
        if login_response_json["status"] != "OK":
            raise MazdaException("Login failed")

        self.logger.debug("Successfully logged in as " + self.email)
        self.access_token = login_response_json["data"]["accessToken"]
        self.access_token_expiration_ts = login_response_json["data"]["accessTokenExpirationTs"]
