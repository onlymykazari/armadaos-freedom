# TB321FU 引导构建与打包

适用于 TB321FU / SM8650 (kirby)。与 Gen4/Gen5 共用
[kShimC 源码](../../lenovo-tb323fu/boot/kshim-success-source/README.md)，使用
`tb321fu_android_defconfig` 机型配置、本机原厂 Android 输入和
[armada-tb321fu](https://github.com/enij90/armada-tb321fu) 移植的设备树。

| 用途 | 构建入口 |
|---|---|
| 4 MiB runtime（tb321fu） | 共享源码树 `tools/build_runtime.py --config configs/tb321fu_android_defconfig` |
| 原厂 Android + kShimC 菜单 | [Gen4 的 build_android_boot.py](../../lenovo-tb322fc/boot/build_android_boot.py)（驱动设备无关，tb321fu 直接复用） |
| Linux 诊断环境或已安装的机身 Linux | [kshim-build-boot.py](../../../system_files/usr/libexec/armada/kshim-build-boot.py)（已接受 `lenovo,tb321fu`/`qcom,sm8650` 配对） |
| 内核与 DTB | 仓库 `packages/kernel`（7.2.6 + 08xx kirby 系列） |

构建前先按[共享依赖准备指引](../../lenovo-tb323fu/boot/kshim-success-source/DEPENDENCIES.md)
恢复 CrDK、picolibc、LVGL 与 libfdt（固定版本见该文件）。

## 1. 内核与 DTB

```sh
just package kernel          # 产出 out/armada-kernel-7.2.6.tar.zst
# DTB 在 tar 包内 lib/modules/<kver>/dtb/qcom/ 下：
#   sm8650-lenovo-tb321fu.dtb       (BOE 面板)
#   sm8650-lenovo-tb321fu-csot.dtb  (CSOT 面板)
```

离线已验证：08xx 补丁全链 0 失败、config 合并全部存活、两个 DTB
`make dtbs` 编译通过。**驱动在真机上的表现沿用 armada-tb321fu 的实机验证
结论**（同一 7.2.6 基线、同一补丁块）。

## 2. 4 MiB runtime

```sh
cd devices/lenovo-tb323fu/boot/kshim-success-source
python3 tools/build_runtime.py --config configs/tb321fu_android_defconfig \
    --out build/tb321fu
# 产物 build/tb321fu/kShimC.bin，必须恰好 4 MiB（脚本自带校验）
```

该 profile 与 Gen4 已验证配置对齐：QCOM_KEYS、QCOM_SPMI、debounce2、
FRAMEBUFFER/LVGL、UI 旋转 270、STAR_CHART 主题、中文、条目详情、
Android 基座、电源/高级/会话设置项；触屏扫描与 SMP 关闭。DPU9 splash
读取（十管线 legacy 拓扑、多层合成、扫描翻转容错）由
`src/dt/splash.c` + `ports/crdk` 补丁提供。

## 3. 原厂 Android 基座打包（离线）

直接使用 Gen4 的驱动（它不含设备特定逻辑，输入由命令行给出）：

```sh
python3 devices/lenovo-tb322fc/boot/build_android_boot.py \
  --stock-boot <96MiB 原厂 boot.img> \
  --runtime   <4MiB kShimC.bin> \
  --packer    <MultiBootKernelPatcher> \
  --unpacker  <UnpackTool> \
  --avbtool   <avbtool 路径> \
  --out       <输出目录>
```

输入约束与 Gen4 相同：stock boot 须为 96 MiB、`ANDROID!`、header v4、
ramdisk_size 0、raw ARM64 Image、带 AVB footer。**TB321FU 的原厂 boot 头
版本尚未核对**：打包脚本会原样报出它读到的头版本，若不是 v4 则说明本机
Android 用的封装不同，先停下核对（SukiSU GKI 版 boot.img 同样须满足该
约束）。请使用与本机 Android 版本匹配的自有原厂 boot 备份。

## 4. Linux 载荷打包

`kshim-build-boot.py` 的两种布局对 tb321fu 均可用（源/派生 DTB 检查已接受
`lenovo,tb321fu` + `qcom,sm8650`）：

```sh
# maintenance：显式给出内核/DTB/initrd/bootargs（诊断或最小载荷）
python3 system_files/usr/libexec/armada/kshim-build-boot.py \
  --layout maintenance \
  --stock-boot <原厂 boot.img> --runtime <kShimC.bin> \
  --linux-image <Image> --dtb <sm8650-lenovo-tb321fu.dtb> \
  --initrd <initramfs.img> \
  --bootargs "root=UUID=<root-uuid> rootflags=subvol=root,noatime rw rootwait console=tty0 quiet" \
  --packer <...> --unpacker <...> --repacker devices/lenovo-tb323fu/boot/star-chart-build/repack_boot_v4.py \
  --avbtool <...> --out <输出目录>

# installed：从已安装 OS 的 /boot 目录选择内核/DTB/initramfs
#   --installer-result <结果文件> --boot-dir </boot>
```

注意：

- **DTB 用打包镜像里的派生 DTB**（脚本自动改写 chosen/bootargs），源 DTB
  不要带 `root=`。
- 面板变体：BOE 单位用 `sm8650-lenovo-tb321fu.dtb`，CSOT 单位用
  `-csot.dtb`（grub-sync 时代的 `tb321fu-panel` 标记机制不适用于本路径，
  换面板 = 换打包镜像里的 DTB 条目）。
- 挂载的 bootargs 中 splash 相关保留区已由设备树声明（`cont_splash_region`
  与 ramoops 均在 `sm8650-lenovo-tb321fu.dts` 中），不要在命令行再传
  memmap。

## 5. 刷写与部署

```sh
adb reboot bootloader
fastboot flash boot_a <打包产物>/boot.img   # 刷写前务必备份原厂 boot_a！
fastboot reboot
```

- 打包产物**未签名**（avbtool 校验用元数据），需要已解锁的 bootloader；
  与 Gen4 相同，原厂 Lenovo 签名无法保留。
- boot_a 为小镜像，ABL fastboot 刷写可靠（super/userdata 的 >4 GiB 损坏
  问题不涉及本流程）。
- 回退：`fastboot flash boot_a <备份的原厂 boot.img>` 即完整还原 Android
  侧；Linux 载荷与根分区不受影响。
- 若 Linux 根是既有 armada-tb321fu 安装（armada_root）：该 OS 的
  `tb321fu-grub-sync` 在关机时会改写 super 开头的 FAT，并在
  `tb321fu-mark-boot-successful` 里给 B 槽置位。切到本路径后 super 已是
  原厂内容，**先按 tb321fu-flash runbook §8 的方式禁用 grub-sync 一次性
  改写，再用 `dualboot-fix.sh` 的思路把成功标记指向 boot_a**（或直接
  mask 这两个服务），否则 ABL 的 7 次重试计数会把启动项打回。

## 6. 更新衔接

系统内 [`armada-kshim-prepare`](../../../system_files/usr/libexec/armada/armada-kshim-prepare)
+ [`armada-kshim-sync`](../../../system_files/usr/libexec/armada/armada-kshim-sync)
负责 OTA 时的打包/写入/回读；sync 的设备门控已扩展到
`lenovo,tb321fu`/`qcom,sm8650`。该流程在 Gen4 上仍标注"待验证"，tb321fu
同属待实机验证范围。

## 7. 诊断

- 菜单黑屏/卡死：用 `KSHIM_BOOTARG_TRACE=1` 重建 runtime（CMake 开关），
  阶段标记落在 `0xa23ff000`（SukiSU ramoops 区尾），Android 侧：
  `su -c 'dd if=/dev/mem bs=4096 skip=$((0xa23ff0)) count=1 2>/dev/null'`。
- 诊断 payload 的内核用 `packages/kernel/config/tb321fu-diag.config.fragment`
  （`KCONFIG_OVERRIDES=<该文件> KCONFIG_RELAXED=1 packages/kernel/scripts/build-kernel.sh`）：
  裁掉媒体/网络文件系统/BPF 等大块以压 Image 体积，但**保留完整 MSM 显示链**
  （KTZ8866 背光 + msm/DSI，simpledrm 作为接管前兜底），ramoops 落在
  0xa2000000，可与 Android 的 /sys/fs/pstore 互读。
