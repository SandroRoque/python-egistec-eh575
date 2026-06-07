#!/bin/bash

# --- Configuration ---
RPMBUILD_DIR="$HOME/rpmbuild"
PACKAGE_NAME="open-fprintd-eh575"

# 1. Find the Spec file
SPEC_FILE=$(ls *.spec 2>/dev/null | head -n 1)

if [ -z "$SPEC_FILE" ]; then
    echo "Error: No .spec file found in current directory."
    exit 1
fi

# 2. Extract Current Version
CURRENT_VERSION=$(grep -m 1 "Version:" "$SPEC_FILE" | awk '{print $2}')
echo "Found spec file: $SPEC_FILE"

# 3. Prompt for New Version
read -p "Enter new version number (current is $CURRENT_VERSION): " NEW_VERSION

if [ -z "$NEW_VERSION" ]; then
    echo "Error: Version cannot be empty."
    exit 1
fi

# 4. Cleanup Old Artifacts
echo "Cleaning up old artifacts..."
rm -f *.rpm
rm -f *.tar.gz
# Optional: Clean rpmbuild SOURCES to avoid confusion (safe to skip if you keep history)
rm -f "$RPMBUILD_DIR/SOURCES/$PACKAGE_NAME-*.tar.gz"

# 5. Update Version in Files
echo "Updating version to $NEW_VERSION..."

# Update .spec file
sed -i "s/^Version:.*/Version:        $NEW_VERSION/" "$SPEC_FILE"

# Update setup.py (if it exists)
if [ -f "setup.py" ]; then
    sed -i "s/version=\".*\"/version=\"$NEW_VERSION\"/" setup.py
fi

# rename open-fprintd-eh575 folder for tarballing
TAR_DIR="open-fprintd-eh575-$NEW_VERSION"
mv open-fprintd-eh575 $TAR_DIR

# Tarball
TAR_NAME="open-fprintd-eh575-$NEW_VERSION.tar.gz"
tar -czvf $TAR_NAME $TAR_DIR/

echo "Copying files to $RPMBUILD_DIR..."
cp "$TAR_NAME" "$RPMBUILD_DIR/SOURCES/"
cp "$SPEC_FILE" "$RPMBUILD_DIR/SPECS/"

# 8. Build SRPM
echo "Building SRPM..."
rpmbuild -bs "$RPMBUILD_DIR/SPECS/$SPEC_FILE"

# 9. Verify Result
if [ $? -eq 0 ]; then
    echo "---------------------------------------"
    echo "Success! SRPM generated at:"
    find "$RPMBUILD_DIR/SRPMS" -name "${PACKAGE_NAME}-${NEW_VERSION}*.src.rpm"
    echo "---------------------------------------"
else
    echo "Build Failed."
    exit 1
fi

# reverse folder renaming
mv $TAR_DIR open-fprintd-eh575