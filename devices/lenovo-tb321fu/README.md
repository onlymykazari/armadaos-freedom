# Lenovo Legion Tab Y700 (2025) / TB321FU — kShimC 多系统启动

适用机型：TB321FU / SM8650（Qualcomm 代号 kirby），Y700 2025（第三代）。
本目录承载 Freedom 对该机型的开机多系统菜单（kShimC）支持。设备树的硬件级
验证（面板、触摸、音频、相机等）由
[armada-tb321fu](https://github.com/enij90/armada-tb321fu) 完成，本移植复用
其 7.2.6 内核补丁与设备树。

## 支持状态

| 部分 | 状态 | 说明 |
|---|---|---|
| kShimC 菜单（tb321fu_android_defconfig） | 离线验证 | 4 MiB runtime 构建通过，无未定义符号；菜单/平台/语言主机测试通过 |
| DPU9 splash 读取（十管线 legacy 拓扑 + 多层合成） | 离线验证 | 按实机寄存器快照实现；**开机菜单上屏待实机确认** |
| 内核 7.2.6 + kirby 补丁系列 | 离线验证 | `packages/kernel/patches` 08xx 块，全链 0 失败 |
| 内核 config（NT36523/KTZ8866/AW882xx 等） | 离线验证 | merge_config 全部存活 |
| 设备树 `sm8650-lenovo-tb321fu[-csot].dtb` | 离线验证 | `make dtbs` 编译通过（BOE 与 CSOT 两个面板变体） |
| 设备 profile / device-env 识别 | 离线验证 | `lenovo,tb321fu` compatible 与机型字符串两条路径 |
| OS 侧设备集成（音频 UCM、传感器、相机、充电策略） | 未开始 |Freedom 的 tb323fu 级别集成尚未移植；可先用 armada-tb321fu 的 OS 作为 Linux 载荷 |
| 真机启动链（ABL → 打包 boot → 菜单 → 双系统） | 待实机 | 见下方清单 |

## 引导链

```
原厂 ABL (abl_a/b, 不改动)
  └─ boot_a：MBKP 打包镜像
       ├─ kShimC runtime (4 MiB) ── 在 ABL splash 上渲染菜单
       ├─ base 条目：原厂 Android 内核（原样交接 → Android 正常启动）
       └─ linux 条目：Linux Image + 派生 DTB (+ 外部 initramfs)
              └─ root= 指向 Linux 根分区
```

与 Gen4/Gen5 相同的机制：菜单是**打包进 Android boot 分区的引导菜单**，
不依赖 GRUB/UEFI，也不做 A/B 槽位切换——Android 侧完全不动（super、userdata、
vbmeta 均保持原厂）。这意味着 tb321fu-flash 项目里那些针对硬分区双系统的
大部分约束（B 侧固件补齐、9008 风险、槽位属性）在本路径下不适用。

Linux 载荷的根分区可选用：

- 既有 armada-tb321fu 安装的 `armada_root`（btrfs，实机验证的 OS）；
- 或未来 Freedom 自身的 tb321fu 安装（待 OS 侧集成完成）。

## 本目录内容

- [boot/README.md](boot/README.md) — runtime 构建、Android 基座打包、Linux
  载荷打包与刷写的完整流程。
- 设备 profile：`system_files/usr/lib/armada/devices/lenovo-tb321fu.conf`，
  由 `device-env` 按 model 字符串或 `lenovo,tb321fu` compatible 选择。
- 固件：`build_files/41-tb321fu-firmware.sh`（需 `ARMADA_ENABLE_TB321FU=1`
  触发，来源见 `build_files/tb321fu-firmware.env` 的固定版本与 SHA-256）。

## 待实机验证清单

1. **菜单上屏**：DPU9 splash 读取在真机上的行为（首个验证点；bootargs 探针
   `KSHIM_BOOTARG_TRACE=1` 会把阶段标记写进 SukiSU ramoops 区，可在
   Android 侧 `dd` 读回定位卡点，见 `kshim-success-source/src/core/main.c`）。
2. **原厂 Android 交接**：base 条目回到 Android 应与原厂无差异。
3. **Linux 载荷交接**：msm 驱动接管 ABL 活动扫描输出（与 armada-tb321fu 的
   GRUB 路径同一约束：DRM 内建、splash 保留区 no-map）。
4. **payload 预算**：boot_a 为 96 MiB；base 内核 + 4 MiB runtime + Linux
   Image/DTB/initramfs 必须放进打包镜像。Image 超预算时用
   `packages/kernel/config/tb321fu-diag.config.fragment` 裁剪诊断载荷。
5. **按键**：音量键在菜单中的行为（tb321fu 的 GPIO 键映射与 Gen4 不同向）。

## 相关资料

- 硬件级验证与单系统安装：armada-tb321fu 项目（README 含完整硬件状态表）。
- 硬分区双系统（槽位切换方案）与踩坑记录：tb321fu-android-armada-dualboot
  与 tb321fu-flash（其 runbook 对 ABL fastboot 行为的结论仍然适用）。
- 共享 kShimC 源码与依赖恢复：`devices/lenovo-tb323fu/boot/kshim-success-source`
  （`DEPENDENCIES.md`）。
