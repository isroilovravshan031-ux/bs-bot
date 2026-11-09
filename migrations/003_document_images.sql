-- Rasm bazada saqlanadi: Railway kabi serverlarda fayl tizimi har deploy'da tozalanadi,
-- "Qayta o'qish" esa asl rasmni talab qiladi. Rasm siqilgan (~150 KB), alohida jadvalda,
-- shunda documents o'qilganda baytlar har safar tortilmaydi.
CREATE TABLE document_images (
    document_id bigint PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    data        bytea NOT NULL
);
