import secrets

def generate_tokens():
    # Generate random secure hex strings
    api_key = f"pk_live_{secrets.token_hex(16)}"
    secret_key = f"sk_live_{secrets.token_hex(32)}"
    
    # These print statements are what actually show up in your terminal!
    print("\n" + "="*50)
    print("✅ NEW PARTNER CREDENTIALS GENERATED")
    print("="*50)
    print(f"API KEY:    {api_key}")
    print(f"SECRET KEY: {secret_key}")
    print("="*50)
    print("⚠️ Give both keys to your partner.")
    print("⚠️ Save both keys to your MongoDB 'registered_partners' collection.\n")

# This tells Python to actually run the function above when you type 'python3'
if __name__ == "__main__":
    generate_tokens()