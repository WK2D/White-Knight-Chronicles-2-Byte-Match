// CDioramaStepSequencer (unit .text 0x008c9d04-0x008ca934, inferred from RTTI).
// Function names and field layouts are placeholders until identified.
#include "wkc2.h"

struct unk_8ca824_entry {
    int id;
    int unk_4;
    int unk_8;
};

struct unk_8ca824_seq {
    char pad_0[0x5dc];
    unk_8ca824_entry entries[8];   // 0x5DC
};

extern "C" {

// 0x008ca824: whether a non-zero id is among the eight entries.
int fn_008ca824(unk_8ca824_seq* seq, int id)
{
    for (int i = 0; i < 8; i++)
        if (seq->entries[i].id != 0 && seq->entries[i].id == id)
            return 1;
    return 0;
}

}
