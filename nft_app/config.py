from web3 import Web3
from solders.pubkey import Pubkey
from solana.rpc.types import TokenAccountOpts
from solana.rpc.api import Client

import logging 
import os
from logging.handlers import TimedRotatingFileHandler

# PnL calculation mode: 'market_price' (current) or 'pool_ratio' (new)
PNL_MODE = 'pool_ratio'

CHAIN_ID_MAP = {
    "BNB": "56",
    "ETH": "1",
    "BAS": "8453",
    "ARB": "42161",
    "LIN": "59144",
    "POL": "137",
    "SON": "146",
    "BER": "80094",
    "UNI": "10143",
    "MON": "143",
    "HYPER": "999",
}

ID_CHAIN_MAP = {
    56: "BNB",
    1: "ETH",
    8453: "BAS",
    42161: "ARB",
    59144: "LIN",
    137: "POL",
    146: "SON",
    80094: "BER",
    10143: "UNI",
    143: "MON",
    999: "HYPER",
}

# API_URLS = {
#     'ETH': 'https://api.etherscan.io/api',
#     'BAS': 'https://api.basescan.org/api',
#     'POL': 'https://api.zkevm.polygonscan.com/api',
#     'BNB': 'https://api.bscscan.com/api',
#     'ARB': 'https://api.arbiscan.io/api',
#     'LIN': 'https://api.lineascan.build/api',
# }

API_URLS = {
    'ETH': f'https://api.etherscan.io/v2/api?chainid=1',
    'BAS': f'https://api.etherscan.io/v2/api?chainid=8453',
    'POL': f'https://api.etherscan.io/v2/api?chainid=137',
    'BNB': f'https://api.etherscan.io/v2/api?chainid=56',
    'ARB': f'https://api.etherscan.io/v2/api?chainid=42161',
    'LIN': f'https://api.etherscan.io/v2/api?chainid=59144',
    'MON': f'https://api.etherscan.io/v2/api?chainid=143',
}

CHAIN_SCAN_URLS = {
    'ETH': 'https://etherscan.io/address/',
    'BAS': 'https://basescan.org/address/',
    'POL': 'https://polygonscan.com/address/',
    'BNB': 'https://bscscan.com/address/',      
    'ARB': 'https://arbiscan.io/address/',
    'LIN': 'https://lineascan.build/address/',
    'SON': 'https://sonicscan.com/address/',
    'MON': 'https://monadvision.com/address/',
}

API_KEYS = {
    'ETH': '82F74VAYNQUN42RVXM37JXUX2F24JPJ8FS',
    'BAS': '82F74VAYNQUN42RVXM37JXUX2F24JPJ8FS',
    'POL': 'UV4PHPTKIZJ3Z1MK9M5TA74Y1RNN29DJ6F',
    'BNB': 'UV4PHPTKIZJ3Z1MK9M5TA74Y1RNN29DJ6F',
    'ARB': 'W961R5X6KISNKFMJ2QWVA7999S2IQDNF6U',
    'LIN': 'W961R5X6KISNKFMJ2QWVA7999S2IQDNF6U',
    'MON': 'W961R5X6KISNKFMJ2QWVA7999S2IQDNF6U',
}

# Config Telegram
TELEGRAM_BOT_TOKEN = "7836597875:AAEbZKTq5OLWoKqRljx4WQXSYY7yMRb5wu4"
TELEGRAM_CHAT_ID = "-1002583666142"
# TELEGRAM_CHAT_ID = "5696892272"

# API Key Infura
API_KEY_INFURA = "92cf6964acae46008404ef57df3020b7"

# PancakeSwap NPM V3 Smart contract
NPM_ADDRESS = Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364")
NPM_ADDRESSES = {
    'ETH': Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"),
    'BNB': Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"),
    'BAS': Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"),
    'ARB': Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"),
    'LIN': Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"),
    'MON': Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364"),
    'POL': Web3.to_checksum_address("0x46A15B0b27311cedF172AB29E4f4766fbE7F4364")
}

AERODROME_NPM_FACTORY_ADDRESSES = {
    'BAS': {
        Web3.to_checksum_address("0x827922686190790b37229fd06084350E74485b72"): Web3.to_checksum_address("0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"),
        Web3.to_checksum_address("0xa990C6a764b73BF43cee5Bb40339c3322FB9D55F"): Web3.to_checksum_address("0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a"),
        Web3.to_checksum_address("0xe1f8cd9AC4e4A65F54f38a5CdAfCA44f6dD68b53"): Web3.to_checksum_address("0xf8f2eB4940cfE7d13603DddD87f123820fC061eF"),
    }
}

AERODROME_NPM_ADDRESSES = {
    chain: list(npm_factory_map.keys())
    for chain, npm_factory_map in AERODROME_NPM_FACTORY_ADDRESSES.items()
}

AERODROME_FACTORY_ADDRESS = next(iter(AERODROME_NPM_FACTORY_ADDRESSES['BAS'].values()))

# PancakeSwap Factory V3 Smart contract
FACTORY_ADDRESS = Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865")
FACTORY_ADDRESSES = {
    'ETH': Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"),
    'BNB': Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"),
    'BAS': Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"),
    'ARB': Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"),
    'LIN': Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"),
    'MON': Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"),
    'POL': Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865")
}

# PancakeSwap Masterchef V3 Smart contract
MASTERCHEF_ADDRESS = Web3.to_checksum_address("0x556B9306565093C855AEA9AE92A594704c2Cd59e")
MASTERCHEF_ADDRESSES = {
    'ETH': Web3.to_checksum_address("0x556B9306565093C855AEA9AE92A594704c2Cd59e"),
    'BNB': Web3.to_checksum_address("0x556B9306565093C855AEA9AE92A594704c2Cd59e"),
    'BAS': Web3.to_checksum_address("0xC6A2Db661D5a5690172d8eB0a7DEA2d3008665A3"),
    'ARB': Web3.to_checksum_address("0x5e09ACf80C0296740eC5d6F643005a4ef8DaA694"),
    'LIN': Web3.to_checksum_address("0x22E2f236065B780FA33EC8C4E58b99ebc8B55c57"),
    'MON': Web3.to_checksum_address("0xe9c7f3196ab8c09f6616365e8873daeb207c0391"),
    'POL': Web3.to_checksum_address("0xe9c7f3196ab8c09f6616365e8873daeb207c0391")
}

RPC_URLS = {
    "BNB": "https://bsc-dataseed.binance.org",
    "ETH": f"https://mainnet.infura.io/v3/{API_KEY_INFURA}",
    "BAS": f"https://base-mainnet.infura.io/v3/{API_KEY_INFURA}",
    "ARB": f"https://arbitrum-mainnet.infura.io/v3/{API_KEY_INFURA}",
    "LIN": f"https://linea-mainnet.infura.io/v3/{API_KEY_INFURA}",
    "MON": f"https://monad-mainnet.infura.io/v3/{API_KEY_INFURA}",
    "POL": f"https://polygon-mainnet.infura.io/v3/{API_KEY_INFURA}"
    
}

ALCHEMY_API_KEY = "xA7-sWnseDzu0v8MsC6J9GpilYRgMtqW"
MORALIS_API_KEY = "7fe3328c4535474d9ac5952534d50fcb"

RPC_BACKUP_LIST = {
    "BNB": [
        f"https://bnb-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}",
        f"https://site1.moralis-nodes.com/bsc/{MORALIS_API_KEY}"
    ],
    "BAS": [
        f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}",
        f"https://site1.moralis-nodes.com/base/{MORALIS_API_KEY}"
    ],
    "ETH": [
        f"https://eth-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    ],
    "ARB": [
        f"https://arb-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    ],
    "LIN": [
        f"https://linea-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    ],
    "POL": [
        f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    ]
}

CHAIN_API_MAP = {
    "BNB": "bsc",
    "ETH": "ethereum",
    "BAS": "base",
    "ARB": "arbitrum",
    "LIN": "linea",
    "MON": "monad",
    "POL": "polygon"
}

CHAIN_NAME_PANCAKE = {
    "BNB": "bsc",
    "ETH": "eth",
    "BAS": "base",
    "ARB": "arb",
    "LIN": "linea",
    "MON": "monad",
    "POL": "polygon"
}

CHAIN_KEY_MORALIS_EVM = {
    "BNB": "bsc",
    "ETH": "eth",
    "BAS": "base",
    "ARB": "arbitrum",
    "LIN": "linea",
    "POL": "polygon",
    "MON": "monad"
}

CAKE_PER_SECOND_ON_CHAIN = {
    "BNB": 0.06644,
    "BAS": 0.07958,
    "ETH": 0.00572,
    "ARB": 0.02745,
}

DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1376423408262189056/MHkTODxyxl0YSry5sey06cD86O2Mww8imltXMV_pxrqdDs-1sAWzx05-wS7JAz8z8zwD"
# DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1377961748925124681/4L4i0oxq6PD1jLlBUV2IxH-G2vobb-ESm2VhKWL30dQztF4sRVg8IkgOoWe4W2EB0IFS"

### SOLANA CONFIG ###
TOKEN_ACCOUNT_OPTS = TokenAccountOpts(program_id=Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"))
SPL_TOKEN_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
PANCAKE_PROGRAM_ID = Pubkey.from_string("HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq")
RAYDIUM_PROGRAM_ID = Pubkey.from_string("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK")
METADATA_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")
# CLIENT = Client("https://dawn-blissful-pallet.solana-mainnet.quiknode.pro/a2995d002f97f0eb9165a1d8ce906d2ce626aa85/")
CLIENT = Client("https://mainnet.helius-rpc.com/?api-key=36925212-7bf9-460c-8a06-70edbb4bb32f")
# WSS_URL = "wss://shy-spring-card.solana-mainnet.quiknode.pro/6a97979ed162924bd71e878f5517215efab54766"
WSS_URL = "wss://hardworking-dark-telescope.solana-mainnet.quiknode.pro/547aae6632a0960cd2c3d93cffed3ab52d15d4a1/"
HELIUS_CLIENT = Client("https://mainnet.helius-rpc.com/?api-key=bb4fcdca-d41d-4930-ada1-6490968dabe4")

MORALIS_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJub25jZSI6IjNkMDQ5OTdhLTE2YWEtNGE5MS1hNzUzLTE5MTE0YzQxMzUwOSIsIm9yZ0lkIjoiNDcxMzUzIiwidXNlcklkIjoiNDg0ODg5IiwidHlwZSI6IlBST0pFQ1QiLCJ0eXBlSWQiOiJjMzc2OGMyZi0xMWEzLTRmNzQtOTEwZC04OTA3ZDhiZTdmMWEiLCJpYXQiOjE3NjY0ODY3NzksImV4cCI6NDkyMjI0Njc3OX0.eDMd69RHoZAM34Q5qgdswpnJuTZSRrK6IXkSVc74xEU"

# Solana RPC endpoints with Helius and Solana mainnet
RPC_SOL_ENDPOINTS = [
    "https://mainnet.helius-rpc.com/?api-key=36925212-7bf9-460c-8a06-70edbb4bb32f",
    "https://solana-mainnet.g.alchemy.com/v2/1IUq3AMnq44C7xi_Q2oX5",
    "https://api.mainnet-beta.solana.com"
]

CLIENTS_SOL_ENDPOINTS = [
    Client("https://mainnet.helius-rpc.com/?api-key=69ddcec4-b718-41c0-9429-069fd24e3091"),
    Client("https://solana-mainnet.g.alchemy.com/v2/objZhkoyHIkTOpSJLSCkC"),
    # Client("https://mainnet.helius-rpc.com/?api-key=bb4fcdca-d41d-4930-ada1-6490968dabe4"),
    Client("https://api.mainnet-beta.solana.com")
]

WS_RPC_URLS = {
    "BNB": f"wss://bsc-mainnet.infura.io/ws/v3/{API_KEY_INFURA}",
    "ETH": f"wss://mainnet.infura.io/ws/v3/{API_KEY_INFURA}",
    "BAS": f"wss://base-mainnet.infura.io/ws/v3/{API_KEY_INFURA}",
    "ARB": f"wss://arbitrum-mainnet.infura.io/ws/v3/{API_KEY_INFURA}",
    "LIN": f"wss://linea-mainnet.infura.io/ws/v3/{API_KEY_INFURA}",
    "POL": f"wss://polygon-mainnet.infura.io/ws/v3/{API_KEY_INFURA}"
}

# QUICKNODE_RPC_URL = "https://shy-spring-card.solana-mainnet.quiknode.pro/6a97979ed162924bd71e878f5517215efab54766"
QUICKNODE_RPC_URL = "https://mainnet.helius-rpc.com/?api-key=69ddcec4-b718-41c0-9429-069fd24e3091"
JUPITER_API_KEY = "87eef807-0114-49ba-a50c-7ec86337a08d"

### TOKEN NATIVE ###
WRAPPED_TOKENS = {
    'BNB': '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c',
    'ETH': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2',
    'BAS': '0x4200000000000000000000000000000000000006',
    'POL': '0x0d500B1d8E8eF31E21C99d1DB9A6444d3ADf1270',
    'ARB': '0x82aF49447D8a07e3bd95BD0d56f35241523fBab1',
    'MON': '0x3bd359C1119dA7Da1D913D1C4D2B7c461115433A',
    'LIN': '0xe5D7C2a44FfDDf6b295A15c148167daaAf5Cf34f',
}

ABI_SLOT0 = [
    {
        "name": "slot0",
        "outputs": [
            {"type": "uint160", "name": "sqrtPriceX96"},
            {"type": "int24", "name": "tick"},
            {"type": "uint16", "name": "observationIndex"},
            {"type": "uint16", "name": "observationCardinality"},
            {"type": "uint16", "name": "observationCardinalityNext"},
            {"type": "uint32", "name": "feeProtocol"},
            {"type": "bool", "name": "unlocked"},
        ],
        "inputs": [],
        "stateMutability": "view",
        "type": "function",
    }
]

ABI_SLOT0_AERODROME = [
    {
        "name": "slot0",
        "outputs": [
            {"type": "uint160", "name": "sqrtPriceX96"},
            {"type": "int24", "name": "tick"},
            {"type": "uint16", "name": "observationIndex"},
            {"type": "uint16", "name": "observationCardinality"},
            {"type": "uint16", "name": "observationCardinalityNext"},
            {"type": "bool", "name": "unlocked"},
        ],
        "inputs": [],
        "stateMutability": "view",
        "type": "function",
    }
]
