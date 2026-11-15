# Talos

Declarative [Talos Linux](https://www.talos.dev) machine configuration for the cluster, built from
composable multi-document patches. Nothing in this directory is applied automatically; configs are
rendered on demand and pushed to nodes with `talosctl`.

## Layout

| Path                                    | Purpose                                                                       |
| --------------------------------------- | ----------------------------------------------------------------------------- |
| `cluster.yaml.j2`                       | Documents applied to every node                                               |
| `controlplane.yaml.j2`                  | Control-plane-only documents, including `machine.type` and the API virtual IP |
| `workers.yaml.j2`                       | Worker-only documents                                                         |
| `nodes/<role>/<node>.yaml.j2`           | Per-node documents (hostname)                                                 |
| `nodes/<role>/<node>.schematic.yaml.j2` | Optional per-node schematic override                                          |
| `schematic.yaml.j2`                     | Shared [Image Factory](https://factory.talos.dev) schematic                   |
| `secrets.yaml.j2`                       | The cluster identity bundle, used only to mint the admin `talosconfig`        |
| `mise.toml`                             | Tasks (`mise run //talos:...`)                                                |

## Nodes

| Node      | Role          | Address         |
| --------- | ------------- | --------------- |
| `talos-1` | Control plane | `192.168.1.210` |
| `talos-2` | Control plane | `192.168.1.213` |
| `talos-3` | Control plane | `192.168.1.215` |
| `talos-4` | Worker        | `192.168.1.211` |
| `talos-5` | Worker        | `192.168.1.212` |
| `talos-6` | Worker        | `192.168.1.214` |

Addresses come from DHCP reservations on the router, so node files carry only a hostname. The
Kubernetes API is reached at `https://192.168.1.209:6443` (`k8s.skynet`), a layer 2 virtual IP held
by whichever control plane is healthy. The router keeps a placeholder DHCP reservation for that
address so it is never handed to another device. Control planes are not tainted: with six small nodes, they run workloads
too.

Every node has two disks. Talos installs to the NVMe; the SATA SSD becomes the `longhorn` user
volume, mounted at `/var/mnt/longhorn`, which Longhorn uses for replicated storage.

## Rendering

`mise run //talos:render-config <node>` builds the final machine config in three layers:

```
talosctl machineconfig patch <(cluster.yaml.j2) \
    -p @<(controlplane.yaml.j2 | workers.yaml.j2) \
    -p @<(nodes/<role>/<node>.yaml.j2)
```

Each layer passes through `minijinja-cli` (strict Jinja templating; the schematic ID arrives as a
`-D` define) before `talosctl` merges them. Later patches strategically merge into earlier ones:
documents with the same kind/name are deep-merged, new documents are appended.

Two conventions keep the layers honest:

- **Directory placement is the single source of truth for a node's role.** The role patch is chosen
  by which `nodes/<role>/` directory contains the node file, and `machine.type` is set by the role
  patch, not the node file. A node cannot claim one role by filename and another by content.
- **Secrets never live in this repo.** Templates reference them as `{{ ENV.NAME }}`. At render
  time `bws run` ([Bitwarden Secrets Manager](https://bitwarden.com/products/secrets-manager))
  supplies the values as environment variables, and the rendered config is piped straight to
  `talosctl` without touching disk.

## Cluster identity

The certificate authorities, join tokens and the etcd encryption key are created once by
`mise run //talos:gen-identity`, which writes them directly into Bitwarden Secrets Manager. The
task refuses to run when the project already holds an identity, because replacing it cuts off
access to a running cluster.

`mise run //talos:talosconfig` mints the admin `talosconfig` (git-ignored, at the repo root) from
that identity. Its client certificate is valid for one year; run the task again to renew it.

## Schematics

The schematic defines the Image Factory build (system extensions, kernel args). The hidden
`//talos:schematic-id` task POSTs it to the factory and gets back a content-addressed ID, which is
templated into the `UnattendedInstallConfig` installer image and used by `download-image` and
`upgrade-node`.

Resolution is per node: `nodes/<role>/<node>.schematic.yaml.j2` wins when present, otherwise the
shared `schematic.yaml.j2` applies. Overrides are complete files, not deltas; they exist for nodes
whose hardware diverges from the fleet. No overrides exist today.

## Gotchas

- `machine.ca` and `cluster.ca` merge as a cert+key **unit**: a patch supplying only `key` blanks
  `crt`. This is why `controlplane.yaml.j2` repeats the `crt` references alongside the keys, and
  why `workers.yaml.j2` carries `crt` only.
- The network link alias `net0` selects the node's single Realtek NIC by driver. A node with a
  second Realtek NIC would need its own selector.
- kexec is disabled (`kernel.kexec_load_disabled`): with the amdgpu driver loaded, a kexec reboot
  hangs these Ryzen APUs with no video until they are power-cycled. Reboots go through the firmware
  instead. The amdgpu extension itself stays, because without it the display goes dark after boot.
- A fresh install wipes the NVMe but never the SATA data disk. Wipe that disk before reinstalling
  a node whose Longhorn data should not survive.

## Common tasks

```sh
mise run //talos:render-config <node>        # render a node's full machine config to stdout
mise run //talos:apply-node <node>           # render and apply (talosctl apply-config)
mise run //talos:upgrade-node <node>         # upgrade Talos using the node's schematic image
mise run //talos:upgrade-k8s <version>       # upgrade Kubernetes across the cluster
mise run //talos:download-image <version>    # fetch a metal ISO from the Image Factory
mise run //talos:talosconfig                 # mint or renew the admin talosconfig
```

`render-config` prints secrets: pipe it into `talosctl`, never into a file or a terminal you share.
Verify a refactor of these templates by confirming
`render-config <node> | talosctl -n <node> apply-config -f /dev/stdin --dry-run` reports
"No changes." on every node.
