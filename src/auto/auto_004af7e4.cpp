// Functions from the provisional unit auto_004af7e4 (.text 0x004af7e4-0x004bf9fc).
// Names and layouts are placeholders until the owning classes are identified.

// The record a matching id is copied from (0x004b2364) and to (0x004b239c).
struct unk_4b2364_record {
    int unk0;
    unsigned char flag;     // 0x04
    char pad5[3];
    int id;                 // 0x08
};

struct unk_4b2364_owner {
    int unk0[2];
    unsigned char flag;     // 0x08
    char pad9[3];
    int id;                 // 0x0C
};

// 0x004b2364: take the record's flag when its id is ours.
int fn_004b2364(unk_4b2364_owner* self, unk_4b2364_record* record)
{
    if (record != 0 && record->id == self->id) {
        self->flag = record->flag;
        return 1;
    }
    return 0;
}

// 0x004b239c: give our flag to the record.
int fn_004b239c(unk_4b2364_owner* self, unk_4b2364_record* record)
{
    if (record) {
        record->flag = self->flag;
        return 1;
    }
    return 0;
}

// 0x004b32e8: clear a flag on each of up to 25 parts.
struct unk_4b32e8_part {
    char pad[29];
    unsigned char active;   // 0x1D
};

struct unk_4b32e8_group {
    char pad[252];
    unk_4b32e8_part* parts[25];   // 0xFC
    unsigned short count;         // 0x160
};

void fn_004b32e8(unk_4b32e8_group* group)
{
    unsigned int count = group->count;
    for (unsigned int i = 0; i < count; ++i)
        group->parts[i]->active = 0;
}
