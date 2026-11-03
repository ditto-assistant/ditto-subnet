mock_provider "google" {}

run "empty_defaults" {
  command = plan
  assert {
    condition     = length(module.vm.snapshot_policy_disks) == 0
    error_message = "Empty default must not attach a boot snapshot policy."
  }
}

run "new_policy_first_plan" {
  command = plan
  variables { enable_snapshots = true }
  assert {
    condition     = module.vm.snapshot_policy_disks[0] == "test-postgres"
    error_message = "The attachment must target the existing instance-named boot disk."
  }
  assert {
    condition     = module.vm.snapshot_policy_disks[1] == "test-postgres-data"
    error_message = "The optional data policy must target the separate data disk."
  }
}
