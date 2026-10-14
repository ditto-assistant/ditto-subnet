import json
from flask import request, jsonify

# New endpoint to attest GitHub account
@app.route('/attest_github', methods=['POST'])
def attest_github():
    data = request.get_json()
    github_login = data.get('github_login')
    miner_hotkey = data.get('miner_hotkey')

    if not github_login or not miner_hotkey:
        return jsonify({'error': 'Missing github_login or miner_hotkey'}), 400

    # Here you would add logic to verify the GitHub account ownership
    # For example, you might check if the user has a specific repository
    # or a commit that proves ownership.

    # Simulating verification success
    verification_success = True  # Replace with actual verification logic

    if verification_success:
        # Record the verified link between GitHub account and miner hotkey
        # This could be a database operation or any other form of persistence
        # For now, we will just return a success message
        return jsonify({'message': 'GitHub account successfully attested', 'github_login': github_login, 'miner_hotkey': miner_hotkey}), 200
    else:
        return jsonify({'error': 'GitHub account verification failed'}), 403
