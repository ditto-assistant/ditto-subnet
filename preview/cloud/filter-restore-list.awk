NR == FNR {
  if ($0 !~ /^[a-z][a-z0-9_]*$/) {
    exit 2
  }
  excluded[$0] = 1
  next
}

$4 == "TABLE" && $5 == "DATA" && $6 == "public" && ($7 in excluded) {
  print ";" $0
  next
}

{ print }
