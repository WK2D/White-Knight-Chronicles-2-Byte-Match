/* Common types for decompiled code. PS3 lv2 ABI: 64-bit registers,
 * 32-bit pointers and long. */
#ifndef WKC2_H
#define WKC2_H

typedef unsigned char      u8;
typedef unsigned short     u16;
typedef unsigned int       u32;
typedef unsigned long long u64;
typedef signed char        s8;
typedef signed short       s16;
typedef signed int         s32;
typedef signed long long   s64;

/* A field at a byte offset, for code whose structures are not recovered yet. */
#define FIELD(type, base, offset) (*(type*)((char*)(base) + (offset)))

#endif
