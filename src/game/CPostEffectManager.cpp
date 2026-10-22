// CPostEffectManager (unit .text 0x004b31ec-0x004b3480, inferred from RTTI).
// Function names and field layouts are placeholders until identified.

struct unk_4b32e8_part {
    char pad[29];
    unsigned char active;   // 0x1D
};

struct unk_4b32e8_group {
    char pad[252];
    unk_4b32e8_part* parts[25];   // 0xFC
    unsigned short count;         // 0x160
};

// 0x004b32e8: clear the active flag of each effect.
void fn_004b32e8(unk_4b32e8_group* group)
{
    unsigned int count = group->count;
    unk_4b32e8_part** part = group->parts;
    for (unsigned int i = 0; i < count; ++i) {
        (*part)->active = 0;
        ++part;
    }
}
