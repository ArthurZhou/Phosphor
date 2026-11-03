/* 关于页 logo 掩码尺寸宏（与 logo_wide.c 配套，168x54 双位平面） */
#ifndef LOGO_WIDE_H
#define LOGO_WIDE_H

#include <stdint.h>

#define LOGO_W   168
#define LOGO_H   54
#define LOGO_WB  21   /* 每行字节数，= (LOGO_W + 7) / 8 */

extern const uint8_t logo_wide[LOGO_H * LOGO_WB];       /* P+wordmark 位平面 */
extern const uint8_t logo_wide_dot[LOGO_H * LOGO_WB];   /* 绿点位平面 */

#endif
