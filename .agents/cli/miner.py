import json
import os

class MinerCLI:
    def __init__(self, hotkey):
        self.hotkey = hotkey
        self.cla_signed = False

    def sign_cla(self):
        # Simulate signing the CLA with the miner hotkey
        if self.hotkey:
            self.cla_signed = True
            self.store_signature()
            print('CLA signed successfully with hotkey.')
        else:
            print('Error: Hotkey is required to sign the CLA.')

    def store_signature(self):
        # Store the signature in a file or database
        signature_data = {
            'hotkey': self.hotkey,
            'signed': True
        }
        with open('cla_signatures.json', 'a') as f:
            json.dump(signature_data, f)
            f.write('\n')

    def run(self):
        # Main execution method
        self.sign_cla()

if __name__ == '__main__':
    miner_hotkey = os.getenv('MINER_HOTKEY')  # Get hotkey from environment variable
    miner_cli = MinerCLI(miner_hotkey)
    miner_cli.run()