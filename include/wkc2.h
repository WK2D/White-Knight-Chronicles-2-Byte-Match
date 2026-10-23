/* Common types for decompiled code. PS3 lv2 ABI: 64-bit registers,
 * 32-bit pointers and long. */
#ifndef WKC2_H
#define WKC2_H

#include <altivec.h>   /* vector float and vec_*: the PPU's AltiVec (VMX) unit */

typedef unsigned char      u8;
typedef unsigned short     u16;
typedef unsigned int       u32;
typedef unsigned long long u64;
typedef signed char        s8;
typedef signed short       s16;
typedef signed int         s32;
typedef signed long long   s64;
typedef vector float       vec;   /* 16 bytes, 16-byte aligned */

/* A byte of bool bitfields (the first declared is the most significant bit):
 * FIELD(BF8, p, off).b2 reads with lbz + rldicl and sets with lbz; ori; stb,
 * as the game's bool flags do. */
#ifdef __cplusplus
struct BF8 { bool b7 : 1; bool b6 : 1; bool b5 : 1; bool b4 : 1; bool b3 : 1; bool b2 : 1; bool b1 : 1; bool b0 : 1; };
#endif

/* A field at a byte offset, for code whose structures are not recovered yet. */
#define FIELD(type, base, offset) (*(type*)((char*)(base) + (offset)))

#endif
