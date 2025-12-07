from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Spectra Bot is alive!"

def run():
    # Only the Flask server runs here
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    # This starts the run function in a non-blocking thread
    t = Thread(target=run)
    t.start()
    
if __name__ == '__main__':
    # This block prevents the thread from being created if run() is called directly
    run()