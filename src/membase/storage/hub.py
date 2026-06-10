from typing import Optional
import requests
import json
import os
from io import BytesIO
from urllib.parse import urlencode
import queue
import threading
import time

import logging

from membase.storage._auth import auth_headers, signer_address

logger = logging.getLogger(__name__)


def _coerce_owner(owner: Optional[str]) -> str:
    """Make sure the owner field sent to the hub is the signer's ETH address.

    The hub now requires ``owner == recovered signer address``. Callers used
    to pass arbitrary strings like ``"noah-2026"``; those will 401. We force
    the owner to the configured wallet and warn loudly if the caller passed
    something else, so we don't silently swallow surprising input.
    """
    signer = signer_address()
    if owner and owner.lower() != signer.lower():
        logger.warning(
            "membase hub: overriding owner=%r with signer wallet %s "
            "(hub requires owner == signer; the legacy 'arbitrary owner' "
            "behaviour is no longer accepted)",
            owner,
            signer,
        )
    return signer


class Client:
    def __init__(self, base_url):
        self.base_url = base_url
        self.upload_queue = queue.Queue()
        self.upload_thread = threading.Thread(target=self._process_upload_queue, daemon=True)
        self.upload_thread.start()
        self.membase_id = os.getenv('MEMBASE_ID', '')

    def _process_upload_queue(self):
        while True:
            try:
                upload_task = self.upload_queue.get()
                if upload_task is None:
                    break

                owner, bucket, filename, msg, event = upload_task
                owner = _coerce_owner(owner)
                meme_struct = {
                    "owner": owner,
                    "bucket": bucket,
                    "id": filename,
                    "message": msg
                }

                meme_struct_json = json.dumps(meme_struct)
                headers = auth_headers(
                    b"upload",
                    extra={"Content-Type": "application/json"},
                )

                response = requests.post(f"{self.base_url}/api/upload", headers=headers, data=meme_struct_json)
                response.raise_for_status()

                res = response.json()
                logger.debug(f"Upload done: {res}")

                event.set()

            except requests.RequestException as err:
                logger.error(f"Error during upload: {err}")
            except Exception as e:
                logger.error(f"Unexpected error in upload queue processing: {e}")
            finally:
                self.upload_queue.task_done()
                time.sleep(0.1)

    def initialize(self, base_url):
        if self.base_url is None:
            self.base_url = base_url

    def upload_hub(self, owner, filename, msg, bucket: Optional[str] = None, wait=True):
        """Add upload task to queue, optionally wait for completion

        Args:
            owner: Owner of the meme. NOTE: the hub now requires this to be
                the signer's wallet address; any other value is overridden
                (with a warning) by the address derived from MEMBASE_ACCOUNT.
            filename: Name of the file
            msg: Message content
            bucket: Bucket name
            wait: Whether to wait for upload completion

        Returns:
            If wait=True, returns upload result; if wait=False, returns queue status
        """
        try:
            owner = _coerce_owner(owner)
            default_bucket = owner
            if self.membase_id != "":
                default_bucket = self.membase_id

            if bucket is None:
                if isinstance(msg, str):
                    try:
                        msg_dict = json.loads(msg)
                        bucket = msg_dict.get("name", default_bucket)
                    except json.JSONDecodeError:
                        bucket = default_bucket
                else:
                    bucket = default_bucket

            # Create an event object for synchronization
            event = threading.Event()
            # Add upload task and event object to queue
            self.upload_queue.put((owner, bucket, filename, msg, event))
            logger.debug(f"Upload task queued: {owner}/{filename}")

            if wait:
                # Wait for upload completion
                event.wait()
                return {"status": "completed", "message": "Upload task completed"}
            else:
                return {"status": "queued", "message": "Upload task has been queued"}

        except Exception as e:
            logger.error(f"Error queueing upload task: {e}")
            return None

    def upload_hub_data(self, owner, filename, data):
        """Upload meme data to the hub server with multipart form."""
        try:
            owner = _coerce_owner(owner)

            # Create a BytesIO stream from the data to simulate a file-like object
            file_stream = BytesIO(data)

            # Prepare the files and data for the multipart request
            files = {
                'file': (filename, file_stream, 'application/octet-stream')
            }
            data = {
                'owner': owner
            }

            # multipart Content-Type is set by requests automatically; we only
            # need to inject Authorization.
            headers = auth_headers(b"upload")

            # Send the POST request to upload data
            response = requests.post(
                f"{self.base_url}/api/uploadData",
                files=files,
                data=data,
                headers=headers,
            )

            # Raise an exception if the request was not successful
            response.raise_for_status()

            # Parse the response JSON into a dictionary
            res = response.json()

            # Log the upload completion
            logger.debug(f"Upload done: {res}")

            # Optionally return the response if needed
            return res

        except requests.RequestException as err:
            logger.error(f"Error during upload: {err}")
            return None

    def list_conversations(self, owner):
        """List all conversations for a given owner."""
        owner = _coerce_owner(owner)
        # Prepare the form data (URL-encoded parameters)
        form_data = {
            'owner': owner,
        }

        # URL encode the form data
        encoded_form = urlencode(form_data)

        try:
            response = requests.post(
                f"{self.base_url}/api/conversation",
                data=encoded_form,
                headers=auth_headers(
                    b"list",
                    extra={"Content-Type": "application/x-www-form-urlencoded"},
                ),
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as err:
            logger.error(f"Error during list conversations: {err}")
            return None

    def get_conversation(self, owner, conversation_id):
        """Get a conversation for a given owner and conversation id."""
        owner = _coerce_owner(owner)
        # Prepare the form data (URL-encoded parameters)
        form_data = {
            'owner': owner,
            'id': conversation_id,
        }

        # URL encode the form data
        encoded_form = urlencode(form_data)

        try:
            response = requests.post(
                f"{self.base_url}/api/conversation",
                data=encoded_form,
                headers=auth_headers(
                    b"list",
                    extra={"Content-Type": "application/x-www-form-urlencoded"},
                ),
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as err:
            logger.error(f"Error during get conversation: {err}")
            return None

    def download_hub(self, owner, filename):
        """Download meme data from the hub server."""
        try:
            owner = _coerce_owner(owner)
            # Prepare the form data (URL-encoded parameters)
            form_data = {
                'id': filename,
                'owner': owner,
            }

            # URL encode the form data
            encoded_form = urlencode(form_data)

            # Log the download action
            logger.debug(f"Downloading {owner} {filename} from hub {self.base_url}")

            # Send the POST request with the encoded form data
            response = requests.post(
                f"{self.base_url}/api/download",
                data=encoded_form,
                headers=auth_headers(
                    b"download",
                    extra={"Content-Type": "application/x-www-form-urlencoded"},
                ),
            )

            # Raise an exception if the request was not successful
            response.raise_for_status()

            # Return the response content (bytes)
            return response.content

        except requests.RequestException as err:
            logger.error(f"Error during download: {err}")
            return None

    def wait_for_upload_queue(self):
        """Wait for all tasks in the upload queue to complete"""
        self.upload_queue.join()

he = os.getenv('MEMBASE_HUB', 'https://testnet.hub.membase.io')
hub_client = Client(he)
