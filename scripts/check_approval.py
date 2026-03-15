"""Check ERC-1155 approval status for funder wallet."""
from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
abi = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    }
]
ctf = w3.eth.contract(
    address=Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
    abi=abi,
)
f = "REDACTED_ADDRESS"
print("Regular:", ctf.functions.isApprovedForAll(f, "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E").call())
print("NegRisk:", ctf.functions.isApprovedForAll(f, "0xC5d563A36AE78145C45a50134d48A1215220f80a").call())
