/* 空的 stdio.h —— 只为让官方 cdc_desc.c 的 #include "stdio.h" 能过
 *
 * 补丁是 freestanding 编译（-nostdlib，没有 libc），cdc_desc.c 虽然 include 了
 * stdio.h，但正文里一次都没用到（序列号是手写的 hex，不是 sprintf），
 * 所以给个空壳就够了。真需要 printf 的时候再看要不要塞个 mini sprintf。
 */
#ifndef DSO_STUB_STDIO_H
#define DSO_STUB_STDIO_H

#include <stddef.h>

#endif
