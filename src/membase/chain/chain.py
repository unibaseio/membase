import pkgutil
import json

from web3 import Web3
from eth_account.messages import encode_defunct
from web3.constants import ADDRESS_ZERO
from web3.contract.contract import ContractFunction
from web3.types import (
    TxParams,
    Wei,
)

from typing import (
    Any,
    Optional
)

import logging
logger = logging.getLogger(__name__)

class Client:
    def __init__(self,  
                 wallet_address: str, 
                 private_key: str, 
                 ep: str = "https://bsc-testnet-rpc.publicnode.com", 
                 membase_contract: str = "0x100E3F8c5285df46A8B9edF6b38B8f90F1C32B7b"
                 ):
        w3 = Web3(Web3.HTTPProvider(ep))
        if w3.is_connected():
            print(f"Connected to the chain: {ep}")
        else:
            print(f"Failed to connect to the chain: {ep}")
            exit(1)

        self.w3 = w3
        self.wallet_address = Web3.to_checksum_address(wallet_address)
        self.private_key = private_key

        contract_json = json.loads(pkgutil.get_data('membase.chain', 'solc/Membase.json').decode())
        self.membase = self.w3.eth.contract(address=membase_contract, abi=contract_json['abi'])
    
    def sign_message(self, message: str)-> str: 
        digest = encode_defunct(text=message)
        signed_message =  self.w3.eth.account.sign_message(digest,self.private_key)
        return signed_message.signature.hex()
    
    def valid_signature(self, message: str, signature: str, wallet_address: str) -> bool: 
        digest = encode_defunct(text=message)
        rec = self.w3.eth.account.recover_message(digest, signature=signature)
        if wallet_address == rec:
            return True
        return False

    def register(self, _uuid: str): 
        addr = self.membase.functions.getAgent(_uuid).call()
        if addr == self.wallet_address:
            return 
        
        if addr != ADDRESS_ZERO:
            raise Exception(f"already register: {_uuid} by {addr}")
        
        return self._build_and_send_tx(
            self.membase.functions.register(_uuid),
            self._get_tx_params(),
        )
    
    def createTask(self, _taskid: str, _price: int): 
        fin, owner, price, value, winner = self.membase.functions.getTask(_taskid).call()
        print(f"task: ", fin, owner, price, value, winner)
        if owner == self.wallet_address:
            return 
        
        if owner != ADDRESS_ZERO:
            raise Exception(f"already register: {_taskid} by {owner}")
        
        return self._build_and_send_tx(
            self.membase.functions.createTask(_taskid, _price),
            self._get_tx_params(),
        )


    def joinTask(self, _taskid: str, _uuid: str): 
        if self.membase.functions.getPermission(_taskid, _uuid).call():
            print(f"already join task: {_taskid}")
            return 

        fin, owner, price, value ,winner= self.membase.functions.getTask(_taskid).call()
        print(f"task: ", fin, owner, price, value, winner)
        
        if fin:
            raise Exception(f"{_taskid} already finish, winner is {winner}")
        
        return self._build_and_send_tx(
            self.membase.functions.joinTask(_taskid, _uuid),
            self._get_tx_params(value=Wei(price)),
        )

    def finishTask(self, _taskid: str, _uuid: str): 
        fin, owner, price, value, winner = self.membase.functions.getTask(_taskid).call()
        print(f"task: ", fin, owner, winner, price, value, winner)
        
        if fin:
            raise Exception(f"{_taskid} already finish, winner is {winner}")
        
        return self._build_and_send_tx(
            self.membase.functions.finishTask(_taskid, _uuid),
            self._get_tx_params(),
        )

    def getTask(self, _taskid: str): 
        fin, owner, price, value, winner = self.membase.functions.getTask(_taskid).call()
        print(f"task: ", fin, owner, price, value, winner)
        return fin, owner, price, value, winner

    def buy(self, _uuid: str, _auuid: str): 
        if self.membase.functions.getPermission(_uuid, _auuid).call():
            return 

        return self._build_and_send_tx(
            self.membase.functions.buy(_uuid, _auuid),
            self._get_tx_params(),
        )
    
    def get_agent(self, _uuid: str) -> str: 
        return self.membase.functions.getAgent(_uuid).call()

    def has_auth(self, _uuid: str, _auuid: str) -> bool: 
        if self.membase.functions.getPermission(_uuid, _auuid).call():
            return True
        
        fin, owner, price, value, winner = self.membase.functions.getTask(_uuid).call()
        if owner == ADDRESS_ZERO:
            return False
        agent_address = self.membase.functions.getAgent(_auuid).call()
        if owner == agent_address:
            return True

    def _display_cause(self, tx_hash: str):
        print(f"check: {tx_hash.hex()}")
        tx = self.w3.eth.get_transaction(tx_hash)

        replay_tx = {
            'to': tx['to'],
            'from': tx['from'],
            'value': tx['value'],
            'data': tx['input'],
        }

        try:
            self.w3.eth.call(replay_tx, tx.blockNumber - 1)
        except Exception as e: 
            print(e)
            raise e

    def _build_and_send_tx(
        self, function: ContractFunction, tx_params: TxParams
    ) :
        """Build and send a transaction."""
        transaction = function.build_transaction(tx_params)

        try: 
            signed_txn = self.w3.eth.account.sign_transaction(transaction, self.private_key)
            rawTX = signed_txn.raw_transaction

            tx_hash = self.w3.eth.send_raw_transaction(rawTX)
            tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if tx_receipt['status'] == 0:
                print("Transaction failed")
                self._display_cause(tx_hash)
            else:
                print(f'Transaction succeeded:: {tx_hash.hex()}')
                logger.debug(f"nonce: {tx_params['nonce']}")
                #gasfee = tx_receipt['gasUsed']*tx_params['gasPrice']
                return "0x"+str(tx_hash.hex())
        except Exception as e:
            raise e

    def _get_tx_params(
        self, value: Wei = Wei(0), gas: Optional[Wei] = None
        ) -> TxParams:
        """Get generic transaction parameters."""
        params: TxParams = {
            "from": self.wallet_address,
            "value": value,
            "nonce": self.w3.eth.get_transaction_count(self.wallet_address) ,
            "gasPrice": self.w3.eth.gas_price,
            "gas": 300_000,
        }

        if gas:
            params["gas"] = gas

        return params


import os
from dotenv import load_dotenv

load_dotenv() 

membase_account = os.getenv('MEMBASE_ACCOUNT')
if not membase_account or membase_account == "":
    print("'MEMBASE_ACCOUNT' is not set, interact with chain")
    exit(1)

membase_secret = os.getenv('MEMBASE_SECRET_KEY')
if not membase_secret or membase_secret == "":
    print("'MEMBASE_SECRET_KEY' is not set")
    exit(1)

membase_id = os.getenv('MEMBASE_ID')
if not membase_id or membase_id == "":
    print("'MEMBASE_ID' is not set, used defined")
    exit(1)

membase_chain = Client(membase_account, membase_secret)