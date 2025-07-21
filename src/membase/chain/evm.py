import threading
import time

from web3 import Web3
from eth_account.messages import encode_defunct
from web3.constants import ADDRESS_ZERO
from web3.contract.contract import ContractFunction
from web3.types import (
    TxParams,
    Wei,
)

from typing import (
    Optional
)


from membase.chain.util import _load_contract_erc20, _sign_transcation

import logging
logger = logging.getLogger(__name__)

# RPC endpoints for different chains
BSC_TESTNET_RPC = [
    "https://bsc-prebsc-dataseed.bnbchain.org"
    "https://bsc-testnet.drpc.org",
    "https://bsc-testnet.public.blastapi.io",
    "https://bsc-testnet-rpc.publicnode.com",
    "https://api.zan.top/bsc-testnet"
]

BSC_MAINNET_RPC = [
    "https://bsc-dataseed1.ninicoin.io",
    "https://bsc-dataseed2.ninicoin.io",
    "https://bsc-dataseed3.ninicoin.io",
    "https://bsc-dataseed4.ninicoin.io",
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed3.binance.org",
    "https://bsc-dataseed4.binance.org",
    "https://bsc.meowrpc.com",
    "https://bsc-pokt.nodies.app",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed2.defibit.io",
    "https://bsc-dataseed3.defibit.io",
    "https://bsc-dataseed4.defibit.io",
]
    
ETH_MAINNET_RPC = []

class BaseClient:
    def __init__(self,  
                 wallet_address: str, 
                 private_key: str, 
                 ep: str = "https://bsc-testnet-rpc.publicnode.com", 
                 check_rpc: bool = True
                 ):
        
        # Determine which RPC list to use based on the endpoint
        if "binance" in ep.lower() or "bsc" in ep.lower():
            if "testnet" in ep.lower() or "test" in ep.lower():
                self.rpc_list = BSC_TESTNET_RPC
            else:
                self.rpc_list = BSC_MAINNET_RPC
        else:
            self.rpc_list = ETH_MAINNET_RPC
            
        # Add user-provided endpoint to the beginning of the list
        if ep not in self.rpc_list:
            self.rpc_list.insert(0, ep)

        # Initialize connection
        self.current_rpc = ep
        self._check_and_switch_rpc()
        if not self.w3:
            raise Exception("Failed to connect to any RPC endpoint")

        self.wallet_address = Web3.to_checksum_address(wallet_address)
        self.private_key = private_key
        self._nonce = self.w3.eth.get_transaction_count(self.wallet_address)

        # Start periodic connection check
        logger.info(f"check_rpc: {check_rpc}")
        self.check_rpc = check_rpc
        if self.check_rpc:
            self.check_interval = 300
            self._stop_check = False
            self._check_thread = threading.Thread(target=self._periodic_connection_check, daemon=True)
            self._check_thread.start()

    def _periodic_connection_check(self):
        """
        Periodically check RPC connection and switch if needed.
        Runs in a separate thread.
        """
        while not self._stop_check:
            try:
                if not self.w3.is_connected():
                    logger.warning(f"RPC connection lost: {self.current_rpc}")
                    self._check_and_switch_rpc()
            except Exception as e:
                logger.warning(f"Error checking RPC connection: {str(e)}")
                self._check_and_switch_rpc()
            
            time.sleep(self.check_interval)

    def stop_periodic_check(self):
        """
        Stop the periodic connection check thread.
        Should be called when the client is no longer needed.
        """
        if not self.check_rpc:
            return
        self._stop_check = True
        if self._check_thread.is_alive():
            self._check_thread.join(timeout=1.0)

    def __del__(self):
        """
        Cleanup when the object is destroyed.
        """
        logger.info("BaseClient __del__")
        if not self.check_rpc:
            return
        self.stop_periodic_check()

    def _check_and_switch_rpc(self):
        """
        Check current RPC connection and switch to another one if needed.
        Returns Web3 instance if connection is successful, None otherwise.
        """
        # First check current connection if exists
        if hasattr(self, 'w3'):
            try:
                if self.w3.is_connected():
                    return
            except Exception:
                pass

        # Try connecting to each RPC node until a successful connection is made
        for rpc in self.rpc_list:
            # Skip current failing RPC
            if rpc == self.current_rpc:
                continue
                
            try:
                w3 = Web3(Web3.HTTPProvider(rpc))
                if w3.is_connected():
                    logger.info(f"Successfully connected to the chain: {rpc}")
                    self.current_rpc = rpc
                    self.w3 = w3
                    break
            except Exception as e:
                logger.warning(f"Failed to connect to {rpc}: {str(e)}")
                continue

    def sign_message(self, message: str)-> str: 
        digest = encode_defunct(text=message)
        self._check_and_switch_rpc()
        signed_message =  self.w3.eth.account.sign_message(digest,self.private_key)
        return signed_message.signature.hex()
    
    def valid_signature(self, message: str, signature: str, wallet_address: str) -> bool: 
        digest = encode_defunct(text=message)
        self._check_and_switch_rpc()
        rec = self.w3.eth.account.recover_message(digest, signature=signature)
        if wallet_address == rec:
            return True
        return False

    def _display_cause(self, tx_hash: str):
        logger.info(f"Display cause for {tx_hash}")
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
            logger.error(f"{tx_hash} fails due to: {e}")
            raise e

    def build_and_send_tx(
        self, function: ContractFunction, tx_params: TxParams
    ) :
        """Build and send a transaction."""
        # Check connection before sending transaction
        self._check_and_switch_rpc()
        if not self.w3.is_connected():
            raise Exception("No available RPC connection")

        transaction = function.build_transaction(tx_params)

        try: 
            signed_txn = self.w3.eth.account.sign_transaction(transaction, self.private_key)
            rawTX = signed_txn.raw_transaction

            tx_hash = self.w3.eth.send_raw_transaction(rawTX)
            tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if tx_receipt['status'] == 0:
                logger.error("Transaction failed")
                self._display_cause(tx_hash)
            else:
                logger.debug(f'Transaction succeeded: {tx_hash.hex()}')
                #logger.debug(f"nonce: {tx_params['nonce']}")
                #gasfee = tx_receipt['gasUsed']*tx_params['gasPrice']
                self._nonce += 1
                return "0x"+str(tx_hash.hex())
        except Exception as e:
            raise e

    def get_tx_params(
        self, value: Wei = Wei(0), gas: Optional[Wei] = None
        ) -> TxParams:
        """Get generic transaction parameters."""

        got_nonce = self.w3.eth.get_transaction_count(self.wallet_address)
        # local nonce is too large or too small, reset it
        if self._nonce > got_nonce + 1 or self._nonce < got_nonce:
            self._nonce = got_nonce
        nonce = max(self._nonce, got_nonce)
        gas_price = min(self.w3.eth.gas_price, 5_000_000_000)
        params: TxParams = {
            "from": self.wallet_address,
            "value": value,
            "nonce": nonce,
            "gasPrice": gas_price,
            "gas": 300_000,
        }

        if gas:
            params["gas"] = gas

        return params
    
    def transfer_asset(self, 
                  received_address :str,
                  token_address :str, 
                  amount: int = 1000000
                  ):
        if token_address == "":
            return self._transfer(received_address, amount)
        else:
            return self._transfer_token(received_address, token_address, amount)
    
    def _transfer_token(self,
                  received_address :str,
                  token_address :str, 
                  amount: int = 1000000
                  ):
        received_address = Web3.to_checksum_address(received_address)
        token_address = Web3.to_checksum_address(token_address)

        token = _load_contract_erc20(self.w3, token_address)

        return self.build_and_send_tx(
            token.functions.transfer(received_address, amount),
            self.get_tx_params(),
        )

    def _transfer(self,
                  received_address :str, 
                  amount: int = 1000000
                  ):
        received_address = Web3.to_checksum_address(received_address)

        bal = self.w3.eth.get_balance(self.wallet_address)
        logger.debug(f"From: {self.wallet_address}, has balance: {bal}")

        bal_before = self.w3.eth.get_balance(received_address)
        logger.debug(f"To: {received_address}, has balance: {bal_before}")

        nonce = max(self._nonce, self.w3.eth.get_transaction_count(self.wallet_address))
        transaction = {
            'chainId': self.config["ChainId"],
            "to": received_address,
            "nonce": nonce,
            "gas": 100_000,
            "gasPrice": self.w3.eth.gas_price,
            "value": amount, 
        }
        #signed_tx = self.w3.eth.account.sign_transaction(tx, private_key)
        try:
            if self.privy_app_id != "" and len(self.private_key) < 64:
                rawTX = _sign_transcation(self.privy_app_id, self.private_key, transaction)
            else:
                signed_txn = self.w3.eth.account.sign_transaction(transaction, self.private_key)
                rawTX = signed_txn.raw_transaction
            tx_hash = self.w3.eth.send_raw_transaction(rawTX)
            tx_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if tx_receipt['status'] == 0:
                logger.error(f"Transfer transaction: {tx_hash.hex()} failed")
                self._display_cause(tx_hash)
            else:
                logger.debug(f'Transfer transaction: {tx_hash.hex()} succeeded')
                return "0x"+str(tx_hash.hex())
        except Exception as e:
            raise e 

    def get_balance(self, 
                wallet_address: str, 
                token_address :str
                ) -> int:
        wallet_address = Web3.to_checksum_address(wallet_address)
        if token_address == "":
            balance = self.w3.eth.get_balance(wallet_address)
        else:   
            balance = self._get_erc20_balance(wallet_address, token_address)
        
        return balance        

    def _get_erc20_balance(self, 
                wallet_address: str, 
                token_address :str
                ) -> int:
        wallet_address = Web3.to_checksum_address(wallet_address)
        if token_address == "":
            balance = self.w3.eth.get_balance(wallet_address)
        else:   
            token_address = Web3.to_checksum_address(token_address)
            balance = _load_contract_erc20(self.w3, token_address).functions.balanceOf(wallet_address).call()
        
        return balance
    
    def check_appraval(self, token_address: str, to_address: str):
        token_address = Web3.to_checksum_address(token_address)
        is_approved = self._is_approved(token_address, to_address)
        logger.info(f"Approved {token_address}: {is_approved}")
        if not is_approved:
            self.approve(token_address, to_address)

    def approve(self, token_address: str, to_address: str, max_approval: Optional[int] = None) -> None:
        """Give an exchange/router max approval of a token."""
        max_approval = self.max_approval_int if not max_approval else max_approval

        function = _load_contract_erc20(self.w3, token_address).functions.approve(
            to_address, max_approval
        )
        logger.warning(f"Approving {token_address} ...")
        self.build_and_send_tx(
            function, 
            self.get_tx_params()
        )
        # Add extra sleep to let tx propagate correctly
        time.sleep(3)

    def _is_approved(self, token_address: str, to_address: str) -> bool:
        amount = (
            _load_contract_erc20(self.w3, token_address)
            .functions.allowance(self.wallet_address, to_address)
            .call()
        )
        if amount >= self.max_approval_check_int:
            return True
        else:
            return False
    
    def get_token_decimals(self, token_address: str) -> int:
        if token_address == "" or token_address == ADDRESS_ZERO:
            return 18
        token_contract = _load_contract_erc20(self.w3, token_address=token_address)
        return token_contract.functions.decimals().call()

    def get_token_supply(self, token_address: str) -> int:
        token_contract = _load_contract_erc20(self.w3, token_address=token_address)
        return token_contract.functions.totalSupply().call()

    def get_tx_info(self, tx_hash: str):
        tx_receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        return tx_receipt