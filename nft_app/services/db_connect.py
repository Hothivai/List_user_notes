import os
from dotenv import load_dotenv
import mysql.connector

load_dotenv()

def get_db_config():
    env = os.getenv("ENV")

    if env == "server":
        DB_CONFIG = {
            "host": os.getenv("SERVER_DB_HOST"),
            "user": os.getenv("SERVER_DB_USER"),
            "password": os.getenv("SERVER_DB_PASS"),
            "database": os.getenv("SERVER_DB_NAME"),
            "port": int(os.getenv("SERVER_DB_PORT", 3307)),
            "ssl_disabled": os.getenv("SERVER_DB_SSL_DISABLED", "False").lower() == "False"
        }
    else:
        DB_CONFIG = {
            "host": os.getenv("LOCAL_DB_HOST"),
            "user": os.getenv("LOCAL_DB_USER"),
            "password": os.getenv("LOCAL_DB_PASS"),
            "database": os.getenv("LOCAL_DB_NAME"),
            "port": int(os.getenv("LOCAL_DB_PORT", 3306)),
            "ssl_disabled": os.getenv("LOCAL_DB_SSL_DISABLED", "False").lower() == "False"
        }
        
    return DB_CONFIG

# Connect to MySQL database
def get_connection():
    DB_CONFIG = get_db_config()
    if DB_CONFIG:
        return mysql.connector.connect(**DB_CONFIG)

