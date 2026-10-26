import argparse
import json
import requests

class MinerCLI:
    def __init__(self, hotkey):
        self.hotkey = hotkey
        self.cla_url = 'https://platform.example.com/api/cla/sign'

    def sign_cla(self):
        payload = {'hotkey': self.hotkey}
        response = requests.post(self.cla_url, json=payload)
        if response.status_code == 200:
            print('CLA signed successfully!')
        else:
            print('Failed to sign CLA:', response.text)

    @staticmethod
    def main():
        parser = argparse.ArgumentParser(description='Miner CLI')
        parser.add_argument('--hotkey', required=True, help='Miner hotkey for signing CLA')
        args = parser.parse_args()
        miner_cli = MinerCLI(args.hotkey)
        miner_cli.sign_cla()

if __name__ == '__main__':
    MinerCLI.main()