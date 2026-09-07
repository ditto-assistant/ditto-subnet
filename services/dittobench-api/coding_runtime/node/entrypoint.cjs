#!/usr/bin/env -S -i PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/node
'use strict';
require('/opt/coding-node/driver.cjs')
  .main()
  .catch(() => {
    process.exitCode = 70;
  });
