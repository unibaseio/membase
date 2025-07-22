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
logger = logging.getLogger(__name__)

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
                meme_struct = {
                    "owner": owner,
                    "bucket": bucket,
                    "id": filename,
                    "message": msg
                }

                meme_struct_json = json.dumps(meme_struct)
                headers = {'Content-Type': 'application/json'}
                
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
            owner: Owner of the meme
            filename: Name of the file
            msg: Message content
            bucket: Bucket name
            wait: Whether to wait for upload completion
            
        Returns:
            If wait=True, returns upload result; if wait=False, returns queue status
        """
        try:
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
            # Create a BytesIO stream from the data to simulate a file-like object
            file_stream = BytesIO(data)
            
            # Prepare the files and data for the multipart request
            files = {
                'file': (filename, file_stream, 'application/octet-stream')
            }
            data = {
                'owner': owner
            }

            # Send the POST request to upload data
            response = requests.post(f"{self.base_url}/api/uploadData", files=files, data=data)

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
        # Prepare the form data (URL-encoded parameters)
        form_data = {
            'owner': owner,
        }
            
        # URL encode the form data
        encoded_form = urlencode(form_data)
        
        try:    
            response = requests.post(f"{self.base_url}/api/conversation", data=encoded_form, headers={'Content-Type': 'application/x-www-form-urlencoded'})
            response.raise_for_status()
            return response.json()
        except requests.RequestException as err:
            logger.error(f"Error during list conversations: {err}")
            return None
    
    def get_conversation(self, owner, conversation_id):
        """Get a conversation for a given owner and conversation id."""
        # Prepare the form data (URL-encoded parameters)
        form_data = {
            'owner': owner,
            'id': conversation_id,
        }
            
        # URL encode the form data
        encoded_form = urlencode(form_data)
        
        try:    
            response = requests.post(f"{self.base_url}/api/conversation", data=encoded_form, headers={'Content-Type': 'application/x-www-form-urlencoded'})
            response.raise_for_status()
            return response.json()
        except requests.RequestException as err:
            logger.error(f"Error during get conversation: {err}")
            return None

    def download_hub(self, owner, filename):
        """Download meme data from the hub server."""
        try:
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
            response = requests.post(f"{self.base_url}/api/download", data=encoded_form, headers={'Content-Type': 'application/x-www-form-urlencoded'})
            
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