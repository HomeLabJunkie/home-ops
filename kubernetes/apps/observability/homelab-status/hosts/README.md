# homelab-status host scripts

The report job (`../app`) reads unraid and pi5 through one restricted SSH key.
On each host the key can only run that host's read-only report script; it
never gets a shell. These files are not applied by Flux: they are installed
by hand, and this directory is their source.

| Host                       | Script             | Installed at                            | Key in                                                              |
| -------------------------- | ------------------ | --------------------------------------- | ------------------------------------------------------------------- |
| unraid (192.168.1.9, root) | `unraid-report.sh` | `/boot/config/homelab-status/report.sh` | `/root/.ssh/authorized_keys` (persists via `/boot/config/ssh/root`) |
| pi5 (192.168.0.209, jeff)  | `pi5-report.sh`    | `/home/jeff/homelab-status/report.sh`   | `/home/jeff/.ssh/authorized_keys`                                   |

The authorized_keys line (the script path differs per host):

```
restrict,from="192.168.1.210,192.168.1.211,192.168.1.212,192.168.1.213,192.168.1.214,192.168.1.215",command="/bin/bash <script path>" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINrphJ3BM9tLcghM08Rdm3fXNhhBH1OFDJRlHp6l2bv7 homelab-status@prodesk
```

- `from=` lists the Talos node IPs: Cilium masquerades pod traffic leaving
  the cluster to the node's address. Update it if node IPs change.
- The private key is the Bitwarden Secrets Manager secret
  `HOMELAB_STATUS_SSH_KEY` (ExternalSecret `homelab-status-ssh`). To rotate:
  generate a new key, replace the secret, swap the public key on both hosts.
- The job pins the hosts' ed25519 keys in `../app/resources/known_hosts`.
- Output is `key<TAB>value` lines; `ctr` lines are `name<TAB>state<TAB>status`.
  After changing a script, copy it to the host and check it with
  `ssh <host> 'bash <script path>'`.
- To revoke: delete the line from both authorized_keys files.
