
import hashlib, secrets
key = secrets.token_hex(32)
key_hash = hashlib.sha256(key.encode()).hexdigest()
print(f"Raw key (save this): {key}")
print(f"Hash (insert to DB): {key_hash}")
