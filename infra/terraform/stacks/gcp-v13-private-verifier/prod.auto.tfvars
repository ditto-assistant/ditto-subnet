# A reviewed intent PR must switch this to true before the first apply, and
# keep it true while resources exist. This flag is not a rollback switch:
# prevent_destroy/deletion_protection require a supervised teardown plan.
enable_v13_private_project = false
# Operator identity is held in the private approval package and supplied as
# a protected plan/apply variable. No human grant is checked into this root.
operators = []
