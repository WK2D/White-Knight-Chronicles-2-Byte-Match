// CPostEffect (unit .text 0x004a92a0-0x004b23c0, inferred from RTTI).
// Function names and field layouts are placeholders until identified.

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
