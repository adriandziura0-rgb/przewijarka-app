plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "pl.przewijak.artykuly"
    compileSdk = 35

    defaultConfig {
        applicationId = "pl.przewijak.artykuly"
        minSdk = 24
        targetSdk = 35
        versionCode = 15701
        versionName = "15.7.1"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

chaquopy {
    defaultConfig {
        version = "3.10"
        pip {
            install("requests>=2.31,<3")
            install("beautifulsoup4>=4.12,<5")
        }
    }
}
