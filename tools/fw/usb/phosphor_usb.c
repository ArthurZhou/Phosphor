/**
  **************************************************************************
  * @file     phosphor_usb.c
  * @brief    Phosphor 数据通道：USB 全速 CDC 设备与采样数据上传
  *
  * 实现说明
  *   USB 协议栈采用 Artery AT32F435_437 官方 USB device 库（usb_core、
  *   usbd_core、usbd_int、usbd_sdr）与官方 CDC 类实现（cdc_class、cdc_desc）。
  *   时钟、GPIO 复用、FIFO 划分、端点使能、SETUP 解析、地址分配、TRDT 与
  *   GCCFG 均由库完成，本文件只负责三点：
  *
  *     1. 板级初始化：48MHz USB 时钟源选择、PA11/PA12 复用、NVIC 使能。
  *     2. 数据源：从固件固定的 RAM 地址读取采样数据并组帧上传。
  *     3. 命令解析：在主循环中读取 CDC OUT 端点数据，解释为采集命令。
  *
  * 数据源（由 APP 固件逆向确定）
  *   0x2000025D   原始采样环形缓冲，16384 x 8bit，由 ADC 经 DMA1_CH0 写入
  *   0x2000425E   屏幕波形数据，301 x 8bit
  *
  * 中断模型
  *   OTGFS1 中断向量在打补丁时指向 usb_irq()，转发给 usbd_irq_handler()。
  *   批量端点每完成一次 IN 传输，dso_cdc_in() 推进下一包，发送过程不占用
  *   主循环时间。
  **************************************************************************
  */
#include "at32f435_437.h"
#include "usb_conf.h"
#include "usb_core.h"
#include "usbd_int.h"
#include "cdc_class.h"
#include "cdc_desc.h"

/* ------------------------------------------------------------------ 数据源 */
#define SAMPLE_ADDR      0x2000025Du
#define SAMPLE_MAX       16384u
#define SCREEN_ADDR      0x2000425Eu
#define SCREEN_N         301u
#define FRAME_N          4096u          /* 单帧样本数，取 4 的倍数便于分块 */

/* ------------------------------------------------------------ 帧标志位定义 */
#define FRAME_LIVE       0x01u          /* 数据取自实时缓冲，非冻结快照 */
#define FRAME_CHK_ADV    0x02u          /* 校验值为发送起始时刻的快照，仅供参照 */
#define FRAME_SCREEN     0x04u          /* 屏幕波形数据 */
#define FRAME_SETTINGS   0x08u          /* 设置结构快照（0x2000A528） */
#define FRAME_IMG        0x20u          /* 屏幕画面块（img.c 组帧，含 8 字节描述） */

/* ------------------------------------------------------------ 数据源选择 */
/*
 * 命令 0x10 选择 0x01 / 0x02 送什么。三者取数据的路径完全不同：
 *   raw    直接读 ADC 环形缓冲（4096 点），发送期间仍被 DMA 覆盖；
 *   screen 读 UI 已算好的 301 点波形列，与屏幕内容一一对应；
 *   image  不在 RAM 里，只能在推屏的那一刻从区域缓冲送出去（img.c）。
 * 0x0D / 0x0E / 0x0F 已由档位直设与 SRAM 读写占用，故数据源用 0x10。
 * 初值写 0 而不是靠 .bss：热复位时 .bss 可能保留，别让上次的选择活过来。
 */
#define SRC_RAW          0u
#define SRC_SCREEN       1u
#define SRC_IMAGE        2u
#define IMG_SWEEP_BANDS  64u            /* 一次扫屏的块预算：页面重绘为 8 条波形
                                         * 带 + 顶栏 + 底栏 + 若干面板，留余量给
                                         * 期间 UI 自身的重绘 */
#define IMG_STREAM_BANDS 0xFFFFFFFFu    /* 直到主机发 0x00 */

/* img.c */
extern volatile uint32_t img_state, img_bands, img_rows, img_budget;
extern volatile uint32_t img_err, img_last;
extern volatile uint8_t  img_busy, img_abort;
extern void img_arm(uint32_t bands);
extern void img_disarm(void);

/* CRM 复位标志（0x40023800 + 0x74）：bit27 POR / bit28 SW / bit29 WDT /
 * bit26 NRST。用于判断"掉线"到底是软件复位、看门狗还是掉电。 */
#define CRM_RST_FLAGS    (*(volatile uint32_t *)(uintptr_t)0x40023874u)

/* ------------------------------------------------------------------ 状态量 */
/* 通过 RAM 中的全局变量对外提供通信状态，供调试器或后续诊断代码读取。 */
volatile uint32_t g_usb_state;      /* 0 = 未初始化，1 = 已初始化 */
volatile uint32_t g_usb_conn;       /* CDC conn_state：0 默认 / 1 已分配地址 / 2 已配置 */
volatile uint32_t g_usb_cfg;        /* dev_config */
volatile uint32_t g_usb_rx;         /* 主机下发字节数累计 */
volatile uint32_t g_usb_tx;         /* 完整帧发送计数 */
volatile uint32_t g_usb_err;        /* 端点忙导致的发送失败次数 */
volatile uint32_t g_usb_sclk;       /* SYSCLK，单位 MHz */
volatile uint32_t g_usb_div;        /* CRM_MISC2.usbdiv 编码 */
volatile uint32_t g_usb_usbclk;     /* 分频后的 USB 时钟，单位 MHz，应为 48 */
volatile uint32_t g_usb_ref;        /* 送入 USB 分频器的时钟，单位 MHz */
volatile uint32_t g_usb_src;        /* 1 = PLL 输出，0 = HICK */
volatile uint32_t g_usb_pll;        /* 由 CRM_PLLCFG 计算出的 PLL 频率，单位 MHz */
volatile uint32_t g_usb_pllcfg;     /* CRM_PLLCFG 原始值 */
volatile uint32_t g_usb_reinit;     /* 控制器状态丢失后重新初始化的次数 */
volatile uint32_t g_usb_inits;       /* usbd_init 调用次数 */
volatile uint32_t g_usb_irq;        /* USB 中断进入次数 */

/* 栈用量观测（诊断用，命令 0x04 上报） */
volatile uint32_t g_stack_base;     /* 原厂 stack_base，由 flash 字面量读出 */
volatile uint32_t g_stack_top;      /* 初始 SP，由向量表首字读出 */
volatile uint32_t g_stack_min;      /* 观测到的最低栈地址 */
volatile uint32_t g_stack_painted;  /* 1 = 已铺设水印 */
volatile uint32_t g_stack_peak;     /* 峰值栈用量，单位字节 */

/* 栈区域取自镜像本身，不写死：避免改动 RAM 布局后测错地方 */
#define STACK_BASE_LIT  (*(volatile uint32_t *)0x08007394u)   /* __user_setup_stackheap r0 */
#define INITIAL_SP_LIT  (*(volatile uint32_t *)0x08007000u)   /* 向量表首字 */
#define STACK_PATTERN   0xA5A5A5A5u

/* ------------------------------------------------------------------ 全局量 */
otg_core_type otg_core_struct;

/* 链接脚本提供的段边界 */
extern uint32_t __data_lma[], __data_start[], __data_end[], __bss_start[], __bss_end[];
extern uint32_t __fault_rec[];      /* 256 字节，不在 .bss 内，跨复位保留 */

/*
 * HardFault 捕获。
 *
 * 原厂固件没有硬件看门狗，任何异常都会停在默认向量（b .）里，表现为插着 USB
 * 时按 AUTO 就彻底死机。这里把 HardFault 向量换成本函数：把现场写进
 * __fault_rec 后立即软件复位，设备可自行恢复，现场可由上位机（命令 0x05）读出。
 * 连续故障超过 3 次则不再复位，避免来回重启。
 *
 * 全程不使用栈（栈本身可能已经溢出），只用 r0-r4。
 */
#define FAULT_REC_LEN  14u          /* 上报字数，见 diag 命令 0x05 */

/*
 * 地址全部写成 movw/movt 立即数，不用 ldr rX, =imm：内联字面量池会让静态
 * 自检的反汇编把池内数据当成指令，误报“可疑绝对地址”。
 * FAULT_REC_ADDR 必须与 link.ld 中 .fault_rec 的地址一致，由 build.sh 校验。
 */
#define FAULT_REC_ADDR 0x2000BF00u
#define REC_LO 0xBF00u
#define REC_HI 0x2000u

__attribute__((section(".text.fault"), naked, used))
void fault_stub(void)
{
    __asm__ volatile(
        "mov    r0, lr\n"
        "tst    r0, #4\n"
        "ite    eq\n"
        "mrseq  r1, msp\n"
        "mrsne  r1, psp\n"
        "movw   r2, #0xBF00\n"              /* __fault_rec */
        "movt   r2, #0x2000\n"
        "movw   r3, #0xED28\n"              /* CFSR */
        "movt   r3, #0xE000\n"
        "ldr    r3, [r3]\n"
        "str    r3, [r2, #0]\n"
        "movw   r3, #0xED2C\n"              /* HFSR */
        "movt   r3, #0xE000\n"
        "ldr    r3, [r3]\n"
        "str    r3, [r2, #4]\n"
        "movw   r3, #0xED34\n"              /* MMFAR */
        "movt   r3, #0xE000\n"
        "ldr    r3, [r3]\n"
        "str    r3, [r2, #8]\n"
        "movw   r3, #0xED38\n"              /* BFAR */
        "movt   r3, #0xE000\n"
        "ldr    r3, [r3]\n"
        "str    r3, [r2, #12]\n"
        "ldr    r3, [r1, #0]\n"             /* R0 */
        "str    r3, [r2, #16]\n"
        "ldr    r3, [r1, #4]\n"             /* R1 */
        "str    r3, [r2, #20]\n"
        "ldr    r3, [r1, #8]\n"             /* R2 */
        "str    r3, [r2, #24]\n"
        "ldr    r3, [r1, #12]\n"            /* R3 */
        "str    r3, [r2, #28]\n"
        "ldr    r3, [r1, #16]\n"            /* R12 */
        "str    r3, [r2, #32]\n"
        "ldr    r3, [r1, #20]\n"            /* LR */
        "str    r3, [r2, #36]\n"
        "ldr    r3, [r1, #24]\n"            /* PC */
        "str    r3, [r2, #40]\n"
        "ldr    r3, [r1, #28]\n"            /* xPSR */
        "str    r3, [r2, #44]\n"
        "ldr    r3, [r2, #48]\n"            /* 故障计数 */
        "add    r3, r3, #1\n"
        "str    r3, [r2, #48]\n"
        "movw   r3, #0x5431\n"              /* "FLT1" */
        "movt   r3, #0x464C\n"
        "str    r3, [r2, #52]\n"
        "ldr    r3, [r2, #48]\n"
        "cmp    r3, #3\n"
        "bhi    1f\n"                       /* 连续死机超过 3 次：原地停住 */
        /* 现场摘要写到 0x2000BF60：__fault_rec 的前 64 字节在下次冷启动时
         * 会被清掉，而 0xBF40 之后的复位原因记录能跨复位保留。所以这里再存
         * 一份精简现场（魔数 + CFSR/HFSR/BFAR/PC/LR），复位后仍读得到——
         * R19 的「--fb 一写就复位但读不到故障现场」就是被冷启动清掉的。 */
        "movw   r0, #0xBF00\n"
        "movt   r0, #0x2000\n"
        "movw   r2, #0xBF60\n"
        "movt   r2, #0x2000\n"
        "movw   r3, #0x5432\n"              /* "FLT2" */
        "movt   r3, #0x464C\n"
        "str    r3, [r2, #0]\n"
        "ldr    r3, [r0, #0]\n"             /* CFSR */
        "str    r3, [r2, #4]\n"
        "ldr    r3, [r0, #4]\n"             /* HFSR */
        "str    r3, [r2, #8]\n"
        "ldr    r3, [r0, #12]\n"            /* BFAR */
        "str    r3, [r2, #12]\n"
        "ldr    r3, [r0, #40]\n"            /* PC */
        "str    r3, [r2, #16]\n"
        "ldr    r3, [r0, #36]\n"            /* LR */
        "str    r3, [r2, #20]\n"
        "movw   r3, #0xED0C\n"              /* SCB->AIRCR */
        "movt   r3, #0xE000\n"
        "movw   r4, #0x0004\n"              /* SYSRESETREQ | 0x05FA0000 */
        "movt   r4, #0x05FA\n"
        "str    r4, [r3]\n"
        "1:\n"
        "b      1b\n"
    );
}

/*
 * 补丁不具备启动代码。首次进入 usb_poll() 时需完成 .data 搬运与 .bss 清零，
 * 否则官方库中带初值的静态量（例如 CDC 的 linecoding）为随机值。
 * 以两个字的魔数判定是否已执行：SRAM 掉电后内容随机，两个字同时匹配的概率
 * 可以忽略。本函数每轮主循环都会被调用，因此不得修改任何运行状态。
 */
#define BOOT_MAGIC0 0x50484F31u     /* "PHO1" */
#define BOOT_MAGIC1 0x55534221u     /* "USB!" */
static volatile uint32_t g_boot_magic[2];

static uint32_t current_sp(void)
{
    uint32_t sp;
    __asm__ volatile("mov %0, sp" : "=r"(sp));
    return sp;
}

/*
 * 铺设栈水印。
 *
 * 只在真正上电（cold start）时执行一次：把当前 SP 以下的空闲栈区全部填成
 * 0xA5A5A5A5，之后每次主循环扫描最低被改写的地址，即为本机的栈峰值。
 * 只填 SP 以下、且留出 128 字节余量，不会破坏调用链的存活栈帧。
 */
static void stack_paint(void)
{
    uint32_t base = STACK_BASE_LIT;
    uint32_t top  = INITIAL_SP_LIT;
    uint32_t sp   = current_sp();
    uint32_t *p;

    if (base < 0x20000000u || base + 512u >= top || top - base > 8192u)
    {
        return;                     /* 读出值不合理，宁可不测 */
    }
    if (sp <= base + 256u || sp > top)
    {
        return;
    }

    for (p = (uint32_t *)base; (uint32_t)p < sp - 128u; p++)
    {
        *p = STACK_PATTERN;
    }

    g_stack_base    = base;
    g_stack_top     = top;
    g_stack_min     = sp - 128u;
    g_stack_peak    = top - (sp - 128u);
    g_stack_painted = 1u;
}

/* 扫描水印，取最低被改写的地址。g_stack_min 只降不升，扫描量随之收敛。 */
static void stack_scan(void)
{
    uint32_t *p;

    if (g_stack_painted == 0u)
    {
        return;
    }
    for (p = (uint32_t *)g_stack_base; (uint32_t)p < g_stack_min; p++)
    {
        if (*p != STACK_PATTERN)
        {
            g_stack_min  = (uint32_t)p;
            g_stack_peak = g_stack_top - (uint32_t)p;
            return;
        }
    }
}

/*
 * 记录本次复位的原因。
 *
 * __fault_rec 之后（偏移 64..79）在故障现场（0..55）之外，上电清零不会覆盖，
 * 因此热复位后仍可读到上次复位是什么原因。布局：
 *   +64 标记 "RST!"  +68 CRM 复位标志  +72 原因变化次数  +76 保留
 *
 * 只在"原因与已记录的不同"时写一次：本函数在每次主循环都会被调用，
 * 若无条件累加会把计数刷成天文数字。
 */
static void log_reset_reason(uint32_t rst, uint8_t warm)
{
    if (__fault_rec[16] == 0x52535421u && __fault_rec[17] == rst
        && __fault_rec[18] != 0u)
    {
        return;
    }
    __fault_rec[16] = 0x52535421u;
    __fault_rec[17] = rst;
    __fault_rec[18] = __fault_rec[18] + 1u;
    __fault_rec[19] = (warm != 0u) ? 0x00010000u : 0x00000001u;
}

static void phosphor_cold_start(void)
{
    uint32_t *d;
    uint32_t rst = CRM_RST_FLAGS;   /* 先抓复位原因，晚读可能被主程序清掉 */
    uint8_t  warm;

    warm = (g_boot_magic[0] == BOOT_MAGIC0 && g_boot_magic[1] == BOOT_MAGIC1)
           ? 1u : 0u;

    if (warm != 0u)
    {
        /* 热复位：保留栈水印与故障现场，只更新复位原因 */
        log_reset_reason(rst, warm);
        return;
    }

    for (d = __bss_start; d < __bss_end; )
    {
        *d++ = 0u;
    }

    {
        uint32_t *s = __data_lma;
        for (d = __data_start; d < __data_end; )
        {
            *d++ = *s++;
        }
    }

    /* 故障记录只在真正上电时清零：热复位后要保留上次死机现场供上位机读取 */
    for (d = __fault_rec; d < __fault_rec + 64u; )
    {
        *d++ = 0u;
    }

    g_boot_magic[0] = BOOT_MAGIC0;
    g_boot_magic[1] = BOOT_MAGIC1;

    log_reset_reason(rst, 0u);
    stack_paint();
}

/*
 * 判断 USB 控制器是否仍处于本补丁初始化后的状态。
 *
 * DWC2 的 GINTMSK（OTG 基址 + 0x18）在 usbd_core_init() 末尾写入中断使能位，
 * 芯片复位后外设寄存器全部归零；总线复位不会清除该寄存器。因此
 * GINTMSK == 0 表示控制器已丢失配置（例如 IAP 跳转后未断电），必须重新
 * 执行初始化，而不能依赖 RAM 中的软件标志。
 */
#define OTG1_BASE   0x50000000u
#define OTG_GINTMSK (*(volatile uint32_t *)(OTG1_BASE + 0x18u))

static int usb_core_alive(void)
{
    return (g_usb_state != 0u) && (OTG_GINTMSK != 0u);
}

/* ------------------------------------------------------------- 参数遥控 */
/*
 * 设备的按键来自 0x2000A53E 的环形队列：20 项缓冲，+0x14 读指针、
 * +0x15 写指针。主循环每轮调用 0x080188A5 出队，按 0x0801F39C 的跳转表
 * （表[键码-1]）分派到对应处理函数，键码 0xFF 表示无按键。
 *
 * 因此遥控参数不必先逆向设置结构的字段语义：调用入队函数 0x080188E9 写入
 * 一个键码，等价于按下对应的键，改档位、重绘与保存全部由设备自身逻辑完成。
 *
 * 入队放在主循环（usb_poll）而不是 USB 中断里执行：入队与出队都会改动读 /
 * 写指针，放在同一上下文串行执行可避免竞争。主循环每轮先跑按键任务、
 * 再跑到本补丁，因此注入的键码在下一轮被分派。
 */
#define KEY_QUEUE      0x2000A53Eu
#define KEY_PUSH       0x080188E8u      /* void push(void *q, uint8_t key) */
#define SETTINGS_ADDR  0x2000A528u      /* 全局设置结构（时基、垂直档位等） */
#define SETTINGS_LEN   64u
#define KEY_NONE       0xFFu

static volatile uint8_t  g_key_req;      /* 待注入的键码，0 = 无 */
static volatile uint32_t g_key_sent;     /* 已注入键码计数 */
static volatile uint32_t g_key_last;     /* 最近注入的键码 */

static void key_push(uint8_t code)
{
    ((void (*)(uint32_t, uint32_t))(KEY_PUSH | 1u))((uint32_t)KEY_QUEUE, code);
    g_key_last = code;
    g_key_sent++;
}

/* ------------------------------------------------ 直接设定时基 / 垂直档位 */
/*
 * 三个参数都是「写字段 → 调原厂施加函数 → 推原厂事件」，逐条对着反汇编
 * 和实机截图核对过。结构基址 0x2000001C（0x0801606C 的 movw/movt 直接确认）。
 *
 * ── 字段（实机截图确认，不是推断）──────────────────────────────────
 *   +0x01  垂直档位   0..8 = 10mV/20mV/50mV/100mV/200mV/500mV/1V/2V/5V
 *   +0x0D  时基       0..23
 *   +0x03  探头倍率   0 = X1，1 = X10
 *   +0x0E  底栏选中项 1..7（**从 1 起**）：1 档位 / 2 探头 / 3 时基 / 4 耦合…
 *   +0x59  信号发生器的波形类型（14 种），不是档位！
 *
 * 判别依据：设备底栏显示 2V/div 时，结构里 +0x01 = 7（表里第 7 项正是 2V），
 * 而同一次读到的 +0x59 = 8。把 +0x59 从 8 改到 0 再抓屏，波形高度与底栏
 * 文字都没有任何变化——所以之前那条「+0x59 是垂直档位」的结论是错的。
 * 0x20004574 也不是垂直刻度表，而是信号发生器那条 301 点波形（0x080164E8
 * 按 +0x59 生成，形状是 exp 曲线）。这两个字段都不要碰。
 *
 * ── 施加方式 ──────────────────────────────────────────────────────
 * 时基：写 +0x0D → 入队事件 3、1（原厂 0x080099F0）。
 * 探头：写 +0x03 → 入队事件 3、1（原厂 0x0800AEDE / 0x080099AC）。
 * 档位：写 +0x01 → 调 0x08008754(+0x01) → 入队事件 3、1
 *       （原厂 0x0800AE74 加档 / 0x0800996E 减档，两条完全对称）。
 *       0x08008754 按索引 tbb 分派到 10 段，每段写 0x4002xxxx 的 GPIO——
 *       那是**前端衰减/增益**的开关，这才是真正的测量配置。少了它，字段改了
 *       也只是一格数字，波形不会变。加档上限 8、减档下限 0，与原厂一致。
 *
 * ── 不再做的事 ────────────────────────────────────────────────────
 * 旧实现靠「强制页面 + 注入拨轮键」去按设备自己的按键。除字段本身就找错了
 * 之外，按键出队（0x08009500）还先查 +0x0B / +0x0C，任一非零就把键丢掉且
 * 不报错；再加上每轮强写页面号会把画面切到设置页、退出时留残影。整段删除。
 */
#define STRUCT_BASE    0x2000001Cu
#define UI_EVENT_Q     0x2000A528u
#define UI_PUSH        0x080188E8u      /* void push(void *q, uint8_t ev) */
#define VGAIN_SET      0x08008754u      /* void f(u8 idx)：配置前端衰减 */
#define TB_FIELD_OFF   0x0Du
#define PR_FIELD_OFF   0x03u
#define VB_FIELD_OFF   0x01u
#define ROLE_TIMEBASE  1u
#define ROLE_VOLTAGE   2u
#define ROLE_PROBE     3u
#define TB_MAX         23u
#define VB_MAX         8u               /* 原厂加档上限就是 8（5V/div） */
#define PR_MAX         1u
/* 原厂改完设置后的事件码（即 draw_block 的 id：3 = 底栏，1 = 波形带） */
#define EV_A           3u
#define EV_B           1u

static volatile uint8_t g_set_role;     /* 0 = 无请求，1 = 时基，2 = 垂直，3 = 探头 */
static volatile uint8_t g_set_target;   /* 目标档位索引 */
static volatile uint8_t g_set_force;    /* 1 = 值没变也重算一次（命令 0x0F） */

static void ui_event(uint8_t ev)
{
    ((void (*)(uint32_t, uint32_t))(UI_PUSH | 1u))((uint32_t)UI_EVENT_Q, ev);
}

/* 每轮主循环调用一次。调用点在 UI 渲染任务之后（见 hook.c）。 */
void phosphor_param_poll(void)
{
    uint8_t role   = g_set_role;
    uint8_t target = g_set_target;
    uint8_t force  = g_set_force;
    uint8_t *base  = (uint8_t *)(uintptr_t)STRUCT_BASE;
    uint8_t cur;

    if (role == 0u)
    {
        return;
    }
    g_set_role  = 0u;
    g_set_force = 0u;

    /* ---------------- 时基 / 探头：写字段 + 推原厂事件 ---------------- */
    if (role == ROLE_TIMEBASE)
    {
        if (target <= TB_MAX)
        {
            base[TB_FIELD_OFF] = target;
            ui_event(EV_A);
            ui_event(EV_B);
        }
        return;
    }
    if (role == ROLE_PROBE)
    {
        if (target <= PR_MAX)
        {
            base[PR_FIELD_OFF] = target;
            ui_event(EV_A);
            ui_event(EV_B);
        }
        return;
    }

    /* ---------------- 垂直档位：写字段 + 配置前端衰减 ---------------- */
    if (role != ROLE_VOLTAGE || target > VB_MAX)
    {
        return;
    }

    cur = base[VB_FIELD_OFF];
    if (cur == target && force == 0u)
    {
        return;                     /* 已是目标档位 */
    }

    base[VB_FIELD_OFF] = target;
    /* 这一步才是真正改测量配置：按档位切换前端衰减/增益。少了它，字段
     * 改了也只是一格数字，波形幅度不变。 */
    ((void (*)(uint32_t))(VGAIN_SET | 1u))((uint32_t)target);

    ui_event(EV_A);
    ui_event(EV_B);
}

/* --------------------------------------------------------- 官方库所需延时 */
/* 仅供 usbd_disconnect() 等流程使用，精度要求低（毫秒级）。 */
void usb_delay_ms(uint32_t ms)
{
    volatile uint32_t i;
    while (ms--)
    {
        for (i = 0; i < 20000u; i++)
        {
            __NOP();
        }
    }
}

void usb_delay_us(uint32_t us)
{
    volatile uint32_t i;
    while (us--)
    {
        for (i = 0; i < 20u; i++)
        {
            __NOP();
        }
    }
}

/* ------------------------------------------------------------- USB 时钟 */
/*
 * 计算 PLL 输出频率。
 * 公式与官方 crm_clocks_freq_get() 的 PLL 分支一致，区别是全程使用 32 位
 * 整数运算：8MHz x pllns(<=127) 不超过 1G，不会溢出，也不引入 64 位除法
 * 辅助函数（补丁不链接 libgcc / compiler-rt）。
 */
static uint32_t pll_freq_calc(void)
{
    static const uint8_t fr_table[6] = {1, 2, 4, 8, 16, 32};
    uint32_t rcs = (CRM->pllcfg_bit.pllrcs != 0u) ? HICK_VALUE : HEXT_VALUE;
    uint32_t ns  = CRM->pllcfg_bit.pllns;
    uint32_t ms  = CRM->pllcfg_bit.pllms;
    uint32_t fr  = fr_table[CRM->pllcfg_bit.pllfr];

    if (ns == 0u || ms == 0u || fr == 0u)
    {
        return 0u;
    }
    return rcs * ns / (ms * fr);
}

/*
 * 由输入频率计算 USB 分频编码。
 *
 * crm_usb_div_type 的编码不是分频值本身：
 *   0x00 = /1.5   0x01 = /1     0x02 = /2.5   0x03 = /2
 *   0x04 = /3.5   0x05 = /3     ...
 * 即整数分频 d 对应编码 2d-1，半档分频 d+0.5 对应编码 2d-2。
 * 计算以 0.5 为步长（half = 2d），避免浮点并覆盖半档。
 */
static uint8_t usb_div_code(uint32_t sclk, uint32_t *out_div_x10)
{
    uint32_t half;
    uint8_t code;

    if (sclk == 0u)
    {
        sclk = 240000000u;
    }

    half = (sclk * 2u + 24000000u) / 48000000u;     /* round(sclk / 24MHz) */
    if (half < 2u)
    {
        half = 2u;
    }
    if (half > 12u)
    {
        half = 12u;
    }

    if ((half & 1u) == 0u)
    {
        code = (uint8_t)(half - 1u);        /* 整数分频 */
    }
    else
    {
        code = (uint8_t)(half - 3u);        /* 半档分频 */
    }

    if (out_div_x10)
    {
        *out_div_x10 = half * 5u;           /* 以 0.1 为单位的分频值 */
    }
    return code;
}

/*
 * 选择 48MHz USB 时钟源。
 *
 * USB 分频器的输入是 PLL 输出而非 SYSCLK，二者之间还存在 AHB 分频，
 * 因此必须以 PLL 频率计算分频值。
 *
 * 若 PLL 无法得到 48MHz（本机型 PLL = 300MHz，/6 = 50MHz、/6.5 = 46.15MHz，
 * 均超出 USB 全速允许的 ±0.25%），则改用 HICK 直出 48MHz，并启用 ACC 以主机
 * SOF 为基准校准 HICK 频率（官方 USB_CLK_HICK 路径）。
 */
static void usb_clock48m_init(void)
{
    crm_clocks_freq_type freq;
    uint32_t pll, ref = 48000000u, div_x10 = 0;
    uint8_t code = 0u;

    crm_clocks_freq_get(&freq);
    g_usb_sclk   = freq.sclk_freq / 1000000u;
    g_usb_pllcfg = CRM->pllcfg;

    pll = pll_freq_calc();
    g_usb_pll = pll / 1000000u;

    if (crm_sysclk_switch_status_get() == CRM_SCLK_PLL
        && pll >= 48000000u && pll <= 400000000u)
    {
        code = usb_div_code(pll, &div_x10);
        if (div_x10)
        {
            ref = pll;
            if (pll / (div_x10 * 10000u) < 475u
                || pll / (div_x10 * 10000u) > 485u)
            {
                ref = 0u;                   /* 无法得到 48MHz */
            }
        }
        else
        {
            ref = 0u;
        }
    }
    else
    {
        ref = 0u;
    }

    if (ref != 0u)
    {
        g_usb_src = 1u;
        crm_usb_clock_source_select(CRM_USB_CLOCK_SOURCE_PLL);
        crm_usb_clock_div_set((crm_usb_div_type)code);
    }
    else
    {
        g_usb_src = 0u;
        ref = 48000000u;
        div_x10 = 10u;
        code = 0u;
        crm_usb_clock_source_select(CRM_USB_CLOCK_SOURCE_HICK);
        crm_periph_clock_enable(CRM_ACC_PERIPH_CLOCK, TRUE);
        acc_write_c1(7980);
        acc_write_c2(8000);
        acc_write_c3(8020);
        acc_sof_select(ACC_SOF_OTG1);                    /* Type-C 接口使用 OTG1 */
        acc_calibration_mode_enable(ACC_CAL_HICKTRIM, TRUE);
    }

    g_usb_ref    = ref / 1000000u;
    g_usb_div    = code;
    g_usb_usbclk = ref / (div_x10 * 100000u);
}

/* --------------------------------------------------------------- GPIO 复用 */
/* 与官方 usb_gpio_config() 的 OTG1 分支一致：PA11 = DM，PA12 = DP，复用 10。 */
static void usb_gpio_config(void)
{
    gpio_init_type gpio_init_struct;

    crm_periph_clock_enable(OTG_PIN_GPIO_CLOCK, TRUE);
    gpio_default_para_init(&gpio_init_struct);

    gpio_init_struct.gpio_drive_strength = GPIO_DRIVE_STRENGTH_STRONGER;
    gpio_init_struct.gpio_out_type  = GPIO_OUTPUT_PUSH_PULL;
    gpio_init_struct.gpio_mode      = GPIO_MODE_MUX;
    gpio_init_struct.gpio_pull      = GPIO_PULL_NONE;

    gpio_init_struct.gpio_pins = OTG_PIN_DP | OTG_PIN_DM;
    gpio_init(OTG_PIN_GPIO, &gpio_init_struct);

    gpio_pin_mux_config(OTG_PIN_GPIO, OTG_PIN_DP_SOURCE, OTG_PIN_MUX);
    gpio_pin_mux_config(OTG_PIN_GPIO, OTG_PIN_DM_SOURCE, OTG_PIN_MUX);
}

/* ------------------------------------------------------------- 数据发送 */
/*
 * 官方 cdc_class 的 in_handler 仅清除发送完成标志。此处不修改官方源文件，
 * 而是复制其 class handler 结构体，将 in_handler 替换为
 * “调用原回调 + 推进下一包”。发送与 USB 事务同速推进，主循环不参与。
 */
static usbd_class_handler phosphor_cdc_handler;
static usb_sts_type (*cdc_in_handler_orig)(void *udev, uint8_t ept_num);
static usb_sts_type (*cdc_out_handler_orig)(void *udev, uint8_t ept_num);

static uint8_t        tx_hdr[8];
static const uint8_t *tx_src;
static uint16_t       tx_n;
static uint16_t       tx_sent;
static uint8_t        tx_active;
static uint8_t        tx_sum;
static uint16_t       tx_seq;
static uint8_t        stream_on;
static uint32_t       stream_tick;
static uint8_t        g_src;            /* 数据源：SRC_RAW / SCREEN / IMAGE */
static uint8_t        src_pending;      /* 0x10 已收到，等待数据源参数 */
static uint8_t        diag_buf[20];     /* 诊断上报（命令 0x04） */
static uint8_t        cmd_pending;      /* 0x06 已收到，等待键码参数 */
/* 0x08 参数缓冲：地址 4 字节（小端）+ 长度 1 字节 */
static uint8_t        ram_pending;
static uint8_t        ram_buf[5];
static uint8_t        ram_idx;
/* 0x0D 参数缓冲：角色 1 字节 + 目标 1 字节 */
static uint8_t        set_pending;
static uint8_t        set_buf[2];
static uint8_t        set_idx;
/* 0x0E 参数缓冲：地址 4 + 长度 1 + 数据（最多 16） */
#define WR_MAX        16u
static uint8_t        wr_pending;
static uint8_t        wr_buf[5 + WR_MAX];
static uint8_t        wr_idx;
static uint8_t        wr_need;      /* 5 + len */
/* 0x0F 参数：角色 1 字节 */
static uint8_t        ap_pending;

static void diag_fill(void)
{
    diag_buf[0]  = (uint8_t)'S';
    diag_buf[1]  = (uint8_t)'T';
    diag_buf[2]  = (uint8_t)'K';
    diag_buf[3]  = (uint8_t)'1';
    diag_buf[4]  = (uint8_t)(g_stack_min  & 0xFFu);
    diag_buf[5]  = (uint8_t)((g_stack_min >> 8)  & 0xFFu);
    diag_buf[6]  = (uint8_t)((g_stack_min >> 16) & 0xFFu);
    diag_buf[7]  = (uint8_t)((g_stack_min >> 24) & 0xFFu);
    diag_buf[8]  = (uint8_t)(g_stack_peak & 0xFFu);
    diag_buf[9]  = (uint8_t)((g_stack_peak >> 8) & 0xFFu);
    diag_buf[10] = (uint8_t)(g_stack_painted & 0xFFu);
    diag_buf[11] = (uint8_t)(g_usb_err & 0xFFu);
    diag_buf[12] = (uint8_t)(g_stack_base & 0xFFu);
    diag_buf[13] = (uint8_t)((g_stack_base >> 8)  & 0xFFu);
    diag_buf[14] = (uint8_t)((g_stack_base >> 16) & 0xFFu);
    diag_buf[15] = (uint8_t)((g_stack_base >> 24) & 0xFFu);
    diag_buf[16] = (uint8_t)(g_stack_top & 0xFFu);
    diag_buf[17] = (uint8_t)((g_stack_top >> 8)  & 0xFFu);
    diag_buf[18] = (uint8_t)((g_stack_top >> 16) & 0xFFu);
    diag_buf[19] = (uint8_t)((g_stack_top >> 24) & 0xFFu);
}

static void tx_pump(void);

static void tx_start(uint16_t n, const uint8_t *src, uint8_t flags)
{
    uint32_t sum = 0;
    uint16_t i;
    uint16_t seq = tx_seq;

    if (tx_active)
    {
        return;
    }
    if (n > SAMPLE_MAX)
    {
        n = SAMPLE_MAX;
    }

    tx_hdr[0] = 0xA5u;
    tx_hdr[1] = 0x5Au;
    tx_hdr[2] = (uint8_t)(n & 0xFFu);
    tx_hdr[3] = (uint8_t)(n >> 8);
    tx_hdr[4] = (uint8_t)(seq & 0xFFu);
    tx_hdr[5] = (uint8_t)(seq >> 8);
    tx_hdr[6] = flags;
    tx_hdr[7] = 0u;

    /* 校验：帧头与样本逐字节累加，取低 8 位 */
    for (i = 0; i < 8u; i++)
    {
        sum += tx_hdr[i];
    }
    for (i = 0; i < n; i++)
    {
        sum += src[i];
    }

    tx_seq++;
    tx_sum    = (uint8_t)sum;
    tx_src    = src;
    tx_n      = n;
    tx_sent   = 0u;
    tx_active = 1u;

    /* 首包必须在此处发出：IN 传输完成后才会产生完成中断。
     * 本函数只在 USB 中断上下文中调用（见 phosphor_cdc_out / phosphor_cdc_in），
     * 无需额外开关中断。 */
    tx_pump();
}

/* 推进一包。可从中断上下文调用。 */
static void tx_pump(void)
{
    const uint8_t *p;
    uint16_t off = tx_sent;
    uint16_t len;

    if (!tx_active)
    {
        return;
    }

    if (off < 8u)
    {
        p   = tx_hdr + off;
        len = (uint16_t)(8u - off);
    }
    else if (off < (uint16_t)(8u + tx_n))
    {
        p   = tx_src + (off - 8u);
        len = (uint16_t)(8u + tx_n - off);
        if (len > USBD_CDC_IN_MAXPACKET_SIZE)
        {
            len = USBD_CDC_IN_MAXPACKET_SIZE;
        }
    }
    else if (off == (uint16_t)(8u + tx_n))
    {
        p   = &tx_sum;
        len = 1u;
    }
    else
    {
        tx_active = 0u;
        g_usb_tx++;
        return;
    }

    if (usb_vcp_send_data(&otg_core_struct.dev, (uint8_t *)p, len) == SUCCESS)
    {
        tx_sent = (uint16_t)(off + len);
    }
    else
    {
        g_usb_err++;
    }
}

/* --------------------------------------------------- 画面块发送（img.c 用） */
/*
 * 三个窄接口，不把 USB 状态机暴露出去：
 *   usb_tx_free()     当前没有整帧在途（画面块只在此时开始，避免与 0x01 抢）
 *   usb_seq_next()    共用同一个帧序号空间，主机看到的是连续序号
 *   usb_img_packet()  发一个包并等到它上总线；超时返回 0
 *
 * 官方 usb_vcp_send_data() 以 g_tx_completed 为闸门，一次只挂一个包，所以
 * “发一包”本身就等价于“等上一包完成”。守卫计数按 144MHz 估约 15-30ms：
 * 正常情况下一个 64 字节包约 1ms 走完，主机不读时在这里放弃而不是挂死。
 */
#define IMG_PKT_GUARD 60000u

int usb_tx_free(void)
{
    return (tx_active == 0u) ? 1 : 0;
}

uint16_t usb_seq_next(void)
{
    return tx_seq++;
}

int usb_tx_ready(void)
{
    cdc_struct_type *p = (cdc_struct_type *)otg_core_struct.dev.class_handler->pdata;
    return (p != 0 && p->g_tx_completed != 0u) ? 1 : 0;
}

int usb_img_packet(const uint8_t *p, uint16_t len)
{
    uint32_t guard = IMG_PKT_GUARD;

    if (len == 0u || len > USBD_CDC_IN_MAXPACKET_SIZE)
    {
        return 0;
    }
    for (;;)
    {
        if (usb_vcp_send_data(&otg_core_struct.dev, (uint8_t *)p, len) == SUCCESS)
        {
            return 1;
        }
        if (img_abort != 0u || --guard == 0u)
        {
            return 0;
        }
    }
}

/* 按当前数据源送一帧。画面不走这里：它没有常驻缓冲，只能由 img.c 在推屏时
 * 逐块送出（见 img_send_band），因此本函数只覆盖 raw / screen 两种。 */
static void emit_one(void)
{
    if (g_src == SRC_SCREEN)
    {
        tx_start((uint16_t)SCREEN_N, (const uint8_t *)SCREEN_ADDR, FRAME_SCREEN);
    }
    else
    {
        tx_start((uint16_t)FRAME_N, (const uint8_t *)SAMPLE_ADDR,
                 FRAME_LIVE | FRAME_CHK_ADV);
    }
}

/*
 * 批量 IN 端点完成回调。
 *
 * 官方实现为“一个包在途”：usb_vcp_send_data() 以 g_tx_completed 为闸门，
 * 且 class_in_handler 每完成一次都会清空 TX FIFO，因此不存在多包排队，
 * 吞吐上限由 USB 事务与中断延迟决定。
 *
 * 连续模式在此处直接起下一帧，不再等待主循环——主循环约 5Hz，
 * 若由它驱动，帧率会被限制在 1 帧/秒左右（实测 3.9KB/s）。
 */
static usb_sts_type phosphor_cdc_in(void *udev, uint8_t ept_num)
{
    usb_sts_type st = cdc_in_handler_orig ? cdc_in_handler_orig(udev, ept_num) : USB_OK;

    if ((ept_num & 0x7Fu) == (USBD_CDC_BULK_IN_EPT & 0x7Fu))
    {
        tx_pump();
        if (stream_on && !tx_active && img_busy == 0u)
        {
            emit_one();
        }
    }
    return st;
}

/* --------------------------------------------------------------- 命令解析 */
/*
 * 命令在 CDC OUT 中断里立即执行。
 *
 * 早期版本在主循环里调用 usb_vcp_get_rxdata()，而主循环约 5Hz，
 * 单帧命令的响应延迟可达 200ms。官方 out_handler 仅置接收标志，
 * 复制结构体后替换即可在数据到达那一刻解析命令。
 */
static void cmd_exec(const uint8_t *cmd, uint16_t n);

static usb_sts_type phosphor_cdc_out(void *udev, uint8_t ept_num)
{
    usb_sts_type st = cdc_out_handler_orig ? cdc_out_handler_orig(udev, ept_num) : USB_OK;
    uint8_t  cmd[USBD_CDC_OUT_MAXPACKET_SIZE];
    uint16_t n;

    if ((ept_num & 0x7Fu) == (USBD_CDC_BULK_OUT_EPT & 0x7Fu))
    {
        n = usb_vcp_get_rxdata(&otg_core_struct.dev, cmd);
        if (n > 0u)
        {
            g_usb_rx += n;
            cmd_exec(cmd, n);
        }
    }
    return st;
}

/* ------------------------------------------------------------------ 初始化 */
static void usb_bringup(void)
{
    phosphor_cdc_handler = cdc_class_handler;                 /* 结构体整体复制 */
    cdc_in_handler_orig  = cdc_class_handler.in_handler;
    cdc_out_handler_orig = cdc_class_handler.out_handler;
    phosphor_cdc_handler.in_handler  = phosphor_cdc_in;
    phosphor_cdc_handler.out_handler = phosphor_cdc_out;

    usb_gpio_config();

    crm_periph_clock_enable(OTG_CLOCK, TRUE);
    usb_clock48m_init();

    nvic_irq_enable(OTG_IRQ, 2, 0);

    tx_active   = 0u;                   /* 热复位后 RAM 中可能残留半帧状态 */
    tx_sent     = 0u;
    stream_on   = 0u;
    stream_tick = 0u;
    g_src       = SRC_RAW;              /* 热复位后别让上次的数据源活过来 */
    src_pending = 0u;
    img_busy    = 0u;
    img_abort   = 0u;

    g_usb_inits++;
    usbd_init(&otg_core_struct,
              USB_FULL_SPEED_CORE_ID,
              USB_ID,
              &phosphor_cdc_handler,
              &cdc_desc_handler);

    g_usb_state = 1u;
}

/* ------------------------------------------------------------- 中断入口 */
/* 独立段并由链接脚本 KEEP 保留：该函数无 C 语言引用，仅由向量表跳入。 */
__attribute__((section(".text.usb_irq")))
void usb_irq(void)
{
    g_usb_irq++;
    usbd_irq_handler(&otg_core_struct);
}

/* --------------------------------------------------------------- 主循环 */
/*
 * 主循环每轮调用一次，仅负责：
 *   1. 首次上电时的 .data/.bss 初始化；
 *   2. 控制器状态丢失后的重新初始化；
 *   3. 状态量与栈用量观测。
 *
 * 命令解析与连续发送均已在 USB 中断中完成，主循环不再参与数据路径。
 */
void usb_poll(void)
{
    phosphor_cold_start();

    if (!usb_core_alive())
    {
        if (g_usb_state)
        {
            g_usb_reinit++;
            g_usb_state = 0u;
        }
        usb_bringup();
        return;
    }

    g_usb_conn = (uint32_t)otg_core_struct.dev.conn_state;
    g_usb_cfg  = otg_core_struct.dev.dev_config;

    /* 键码注入在主循环上下文执行，避开与出队的竞争；按键任务在下一轮取走 */
    if (g_key_req != 0u)
    {
        uint8_t k = g_key_req;
        g_key_req = 0u;
        key_push(k);
    }

    /* 档位直设的推动在 hook.c 末尾（UI 任务之后）调用 phosphor_param_poll() */

    stack_scan();
}

/* --------------------------------------------------------------- 命令解析 */
/*
 * 命令在 CDC OUT 中断里立即执行。
 *
 * 早期版本在主循环里调用 usb_vcp_get_rxdata()，而主循环约 5Hz，
 * 单帧命令的响应延迟可达 200ms，连续模式也退化到约 1 帧/秒。
 * 官方 out_handler 仅置接收标志，复制结构体后替换即可在主机数据
 * 到达的那一刻完成解析。此处定义在 diag_fill 之后以引用诊断量。
 */
static void cmd_exec(const uint8_t *cmd, uint16_t n)
{
    uint16_t i;

    for (i = 0; i < n; i++)
    {
        /* 画面块正在阻塞发送（img.c 在 push_band 里发包）：此时端点被占，
         * 任何 tx_start 都会失败并把整帧卡死。只受理停止命令，其余丢弃。 */
        if (img_busy != 0u)
        {
            if (cmd[i] == 0x00u)
            {
                img_abort = 1u;
                stream_on = 0u;
            }
            continue;
        }

        /* 0x10 带一个字节参数（数据源），可能分跨多包到达 */
        if (src_pending != 0u)
        {
            src_pending = 0u;
            if (cmd[i] <= SRC_IMAGE)
            {
                g_src     = cmd[i];
                stream_on = 0u;      /* 换源必须停掉旧的连续模式 */
                img_disarm();        /* 换源同时停掉画面流 */
            }
            continue;
        }

        /* 0x06 带一个字节参数（键码）。参数可能与命令字节分在两包里到达，
         * 因此用 cmd_pending 记住“下一个字节是参数”。 */
        if (cmd_pending == 0x06u)
        {
            cmd_pending = 0u;
            if (cmd[i] != KEY_NONE)
            {
                g_key_req = cmd[i];
            }
            continue;
        }

        /* 0x08 带五个字节参数（地址 4 + 长度 1），可能分跨多包到达 */
        if (ram_pending != 0u)
        {
            ram_buf[ram_idx++] = cmd[i];
            if (ram_idx < 5u)
            {
                continue;
            }
            ram_pending = 0u;
            {
                uint32_t addr = (uint32_t)ram_buf[0] |
                                ((uint32_t)ram_buf[1] << 8) |
                                ((uint32_t)ram_buf[2] << 16) |
                                ((uint32_t)ram_buf[3] << 24);
                uint32_t len  = ram_buf[4];

                if (len == 0u)
                {
                    len = 256u;
                }
                /* 只放行实测可用的片内 SRAM 0x20000000..0x2000BFFF（48KB，
                 * 上界由原厂初始 SP 0x2000B580 与写入实测共同确定），以及
                 * USD 区 0x1FFFC000..0x1FFFC1FF（读 EOPB0）与 CRM 复位标志
                 * 0x40023800..0x400238FF（只读，用于判断复位原因）。
                 * 越界读会当场 HardFault 复位——诊断工具不该反过来把设备
                 * 弄崩，所以这里比 RAM 上界更严格。 */
                int ok = (addr >= 0x20000000u && addr < 0x2000C000u &&
                          len <= 0x2000C000u - addr) ||
                         (addr >= 0x1FFFC000u && addr <= 0x1FFFC1FFu &&
                          len <= 0x1FFFC200u - addr) ||
                         (addr >= 0x40023800u && addr <= 0x400238FFu &&
                          len <= 0x40023900u - addr);
                if (ok && len <= 512u)
                {
                    tx_start((uint16_t)len, (const uint8_t *)(uintptr_t)addr,
                             0u);
                }
            }
            continue;
        }

        /* 0x0D 带两个字节参数（角色 + 目标），可能分跨多包到达 */
        if (set_pending != 0u)
        {
            set_buf[set_idx++] = cmd[i];
            if (set_idx < 2u)
            {
                continue;
            }
            set_pending = 0u;
            {
                uint8_t role   = set_buf[0];
                uint8_t target = set_buf[1];
                int ok = (role == ROLE_TIMEBASE && target <= TB_MAX) ||
                         (role == ROLE_VOLTAGE  && target <= VB_MAX) ||
                         (role == ROLE_PROBE    && target <= PR_MAX);
                if (ok)
                {
                    g_set_target = target;
                    g_set_role   = role;
                    g_set_force  = 0u;
                }
            }
            continue;
        }

        /* 0x0F 带一个字节参数（角色）：按当前字段值重新应用一次 */
        if (ap_pending != 0u)
        {
            uint8_t role = cmd[i];
            ap_pending = 0u;
            if (role == ROLE_TIMEBASE || role == ROLE_VOLTAGE ||
                role == ROLE_PROBE)
            {
                const uint8_t *base = (const uint8_t *)(uintptr_t)STRUCT_BASE;
                uint8_t off = (role == ROLE_TIMEBASE) ? TB_FIELD_OFF :
                              (role == ROLE_VOLTAGE)  ? VB_FIELD_OFF :
                                                        PR_FIELD_OFF;
                g_set_target = base[off];
                g_set_role   = role;
                g_set_force  = 1u;      /* 值没变也要重算一次 */
            }
            continue;
        }

        /* 0x0E 写 SRAM：地址 4 + 长度 1 + 数据，可能分跨多包到达 */
        if (wr_pending != 0u)
        {
            wr_buf[wr_idx++] = cmd[i];
            if (wr_idx == 5u)
            {
                uint8_t len = wr_buf[4];
                if (len == 0u || len > WR_MAX)
                {
                    wr_pending = 0u;         /* 非法长度，丢弃 */
                    continue;
                }
                wr_need = (uint8_t)(5u + len);
            }
            if (wr_need != 0u && wr_idx >= wr_need)
            {
                uint32_t addr = (uint32_t)wr_buf[0] |
                                ((uint32_t)wr_buf[1] << 8) |
                                ((uint32_t)wr_buf[2] << 16) |
                                ((uint32_t)wr_buf[3] << 24);
                uint32_t len  = wr_buf[4];
                int ok = (addr >= 0x20000000u && addr < 0x20000300u &&
                          len <= 0x20000300u - addr) ||
                         (addr >= 0x20004F80u && addr < 0x20005000u &&
                          len <= 0x20005000u - addr);
                if (ok)
                {
                    uint8_t *dst = (uint8_t *)(uintptr_t)addr;
                    uint32_t k;
                    for (k = 0; k < len; k++)
                    {
                        dst[k] = wr_buf[5u + k];
                    }
                }
                wr_pending = 0u;
                wr_need    = 0u;
            }
            continue;
        }

        switch (cmd[i])
        {
            case 0x00u:                          /* 停止连续发送 / 停止画面流 */
                stream_on = 0u;
                img_disarm();
                break;
            case 0x01u:                          /* 单帧：按当前数据源 */
                if (g_src == SRC_IMAGE)
                {
                    img_arm(IMG_SWEEP_BANDS);    /* 武装一次扫屏 */
                }
                else
                {
                    emit_one();
                }
                break;
            case 0x02u:                          /* 连续：按当前数据源 */
                if (g_src == SRC_IMAGE)
                {
                    stream_on = 0u;
                    img_arm(IMG_STREAM_BANDS);
                }
                else
                {
                    stream_on = 1u;
                    stream_tick = 0u;
                    if (!tx_active)
                    {
                        emit_one();
                    }
                }
                break;
            case 0x03u:                          /* 单帧屏幕波形（不论当前数据源） */
                tx_start((uint16_t)SCREEN_N, (const uint8_t *)SCREEN_ADDR,
                         FRAME_SCREEN);
                break;
            case 0x04u:                          /* 诊断：栈用量等 20 字节 */
                diag_fill();
                tx_start((uint16_t)sizeof(diag_buf), diag_buf, 0u);
                break;
            case 0x05u:                          /* HardFault 现场，56 字节 */
                tx_start((uint16_t)(FAULT_REC_LEN * 4u),
                         (const uint8_t *)__fault_rec, 0u);
                break;
            case 0x06u:                          /* 注入键码，下一字节为键码 */
                cmd_pending = 0x06u;
                break;
            case 0x08u:                          /* 读 SRAM，后随地址 4 + 长度 1 */
                ram_pending = 1u;
                ram_idx = 0u;
                break;
            case 0x07u:                          /* 设置结构快照，64 字节 */
                tx_start((uint16_t)SETTINGS_LEN, (const uint8_t *)SETTINGS_ADDR,
                         FRAME_SETTINGS | FRAME_CHK_ADV);
                break;
            case 0x0Du:                          /* 直接设定档位：角色 + 目标 */
                set_pending = 1u;
                set_idx = 0u;
                break;
            case 0x0Eu:                          /* 写 SRAM：地址 + 长度 + 数据 */
                wr_pending = 1u;
                wr_idx = 0u;
                wr_need = 0u;
                break;
            case 0x0Fu:                          /* 按当前字段值重新应用 */
                ap_pending = 1u;
                break;
            case 0x10u:                          /* 选择数据源，后随 1 字节 */
                src_pending = 1u;
                break;
            default:
                break;
        }
    }
}
