import sqlite3
import os
from app.config import DATA_DIR
from app import timeutil

DB_PATH = os.path.join(DATA_DIR, 'data.db')

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hourly_traffic (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER,
            month INTEGER,
            day INTEGER,
            hour INTEGER,
            rx_bytes INTEGER DEFAULT 0,
            tx_bytes INTEGER DEFAULT 0,
            UNIQUE(year, month, day, hour)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS meta_info (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    cursor.execute('SELECT value FROM meta_info WHERE key = "install_time"')
    if not cursor.fetchone():
        cursor.execute('INSERT INTO meta_info (key, value) VALUES (?, ?)', 
                       ('install_time', timeutil.now().isoformat()))
    conn.commit()
    conn.close()

def save_traffic_increment(rx_inc, tx_inc, year, month, day, hour):
    if rx_inc == 0 and tx_inc == 0:
        return
        
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT OR IGNORE INTO hourly_traffic (year, month, day, hour, rx_bytes, tx_bytes)
        VALUES (?, ?, ?, ?, 0, 0)
    ''', (year, month, day, hour))
    
    cursor.execute('''
        UPDATE hourly_traffic 
        SET rx_bytes = rx_bytes + ?, tx_bytes = tx_bytes + ?
        WHERE year = ? AND month = ? AND day = ? AND hour = ?
    ''', (rx_inc, tx_inc, year, month, day, hour))
    
    conn.commit()
    conn.close()

def get_meta_value(key, default=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM meta_info WHERE key = ?', (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else default

def set_meta_value(key, value):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO meta_info (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    ''', (key, value))
    conn.commit()
    conn.close()
