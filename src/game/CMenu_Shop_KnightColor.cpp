// CMenu_Shop_KnightColor (unit .text 0x007f1c84-0x007f21fc, inferred from RTTI).
// Function names and field layouts are placeholders until identified.
#include "wkc2.h"

struct unk_7f1cf8_menu {
    char pad_0[0x37c];
    u8 changed;        // 0x37C
    u32 current[4];    // 0x380
    u32 original[4];   // 0x390
};

extern "C" {

// 0x007f1cf8: count the colour slots that differ from the original.
void fn_007f1cf8(unk_7f1cf8_menu* menu)
{
    menu->changed = 0;
    for (int i = 0; i < 4; i++)
        if (menu->current[i] != menu->original[i])
            menu->changed++;
}

}
