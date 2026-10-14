import json
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Endpoint to attest GitHub account
@app.route('/attest_github', methods=['POST'])
def attest_github():
    data = request.json
    github_login = data.get('github_login')
    miner_hotkey = data.get('miner_hotkey')

    if not github_login or not miner_hotkey:
        return jsonify({'error': 'Missing github_login or miner_hotkey'}), 400

    # Here you would implement the logic to verify the GitHub account
    # For example, you could use GitHub's API to check if the account exists
    response = requests.get(f'https://api.github.com/users/{github_login}')

    if response.status_code != 200:
        return jsonify({'error': 'GitHub account does not exist'}), 404

    # Logic to link the GitHub account with the miner hotkey
    # This could involve saving the association in a database
    # For demonstration, we will just return a success message
    # In a real application, you would save this association
    return jsonify({'message': 'GitHub account successfully linked to miner hotkey'}), 200

if __name__ == '__main__':
    app.run(debug=True)