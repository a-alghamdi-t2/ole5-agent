# docling-serve with Arabic OCR.
#
# The stock image ships tesseract-langpack-eng only, so Arabic pages OCR into
# Latin gibberish. Adding the Arabic langpack is all Tesseract needs; its model
# data lives in the image, so no weights are fetched at runtime.
FROM quay.io/docling-project/docling-serve:latest

USER 0
RUN dnf install -y tesseract-langpack-ara && dnf clean all
USER 1001
