// Unit auto_00a25abc (.text 0x00a25abc-0x00a25f44); no class known yet.
// Names and layouts are placeholders until identified.
#include "wkc2.h"

struct unk_a25abc_entry {     // 72 bytes
    char pad_0[8];
    void* object;             // 0x08
    int id;                   // 0x0C
    char pad_10[56];
};

struct unk_a25abc_table {
    int unk_0;
    unk_a25abc_entry* entries;   // 0x04
    int count;                   // 0x08
};

extern unk_a25abc_table g_00a25abc_table;

extern "C" {

// 0x00a25abc: whether a live entry has the given id.
int fn_00a25abc(int id)
{
    for (int i = 0; i < g_00a25abc_table.count; i++)
        if (g_00a25abc_table.entries[i].object != 0 && g_00a25abc_table.entries[i].id == id)
            return 1;
    return 0;
}

}
