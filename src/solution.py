import json
import os

class BountyBoard:
    def __init__(self, storage_path="bounties.json"):
        self.storage_path = storage_path
        self.bounties = []
        self.load()

    def load(self):
        if os.path.exists(self.storage_path):
            with open(self.storage_path, "r") as f:
                self.bounties = json.load(f)

    def save(self):
        with open(self.storage_path, "w") as f:
            json.dump(self.bounties, f, indent=2)

    def create_bounty(self, title, scope, reward, reviewer):
        bounty = {
            "id": len(self.bounties) + 1,
            "title": title,
            "scope": scope,
            "reward": reward,
            "reviewer": reviewer,
            "state": "open",
            "claimant": None,
            "payment_status": "unpaid"
        }
        self.bounties.append(bounty)
        self.save()
        return bounty["id"]

    def claim_bounty(self, bounty_id, claimant):
        for b in self.bounties:
            if b["id"] == bounty_id:
                if b["state"] != "open":
                    raise ValueError("Bounty not open for claiming")
                b["claimant"] = claimant
                b["state"] = "claimed"
                self.save()
                return True
        raise ValueError("Bounty not found")

    def close_bounty(self, bounty_id, paid=False):
        for b in self.bounties:
            if b["id"] == bounty_id:
                if paid:
                    b["state"] = "paid"
                    b["payment_status"] = "paid"
                else:
                    b["state"] = "closed_without_payment"
                    b["payment_status"] = "unpaid"
                self.save()
                return True
        raise ValueError("Bounty not found")
