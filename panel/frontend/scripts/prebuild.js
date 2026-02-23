#!/usr/bin/env node

import fs from 'fs';
import path from 'path';
import crypto from 'crypto';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const INTERNAL_DIR = path.join(__dirname, '..', 'src', 'pages', '_internal');
const MOD_FILE = path.join(INTERNAL_DIR, 'mod.enc');
const OUTPUT_FILE = path.join(INTERNAL_DIR, 'ExtPage.tsx');
const INDEX_FILE = path.join(INTERNAL_DIR, 'index.ts');

const STORES_DIR = path.join(__dirname, '..', 'src', 'stores');
const API_DIR = path.join(__dirname, '..', 'src', 'api');
const STORE_ENC = path.join(STORES_DIR, '_ext.enc');
const STORE_OUT = path.join(STORES_DIR, '_extStore.ts');
const API_ENC = path.join(API_DIR, '_ext.enc');
const API_OUT = path.join(API_DIR, '_ext.ts');

function deriveKey(password) {
    return crypto.createHash('sha256').update(password).digest();
}

function process_data(dataB64, key) {
    const keyBytes = deriveKey(key);
    const data = Buffer.from(dataB64, 'base64');
    const nonce = data.subarray(0, 12);
    const ciphertext = data.subarray(12, -16);
    const tag = data.subarray(-16);
    
    const decipher = crypto.createDecipheriv('aes-256-gcm', keyBytes, nonce);
    decipher.setAuthTag(tag);
    
    let result = decipher.update(ciphertext);
    result = Buffer.concat([result, decipher.final()]);
    
    return result.toString('utf-8');
}

function createStubs() {
    if (!fs.existsSync(INTERNAL_DIR)) {
        fs.mkdirSync(INTERNAL_DIR, { recursive: true });
    }
    
    fs.writeFileSync(OUTPUT_FILE, `export default function ExtPage() {
    return null
}
`);
    
    fs.writeFileSync(INDEX_FILE, `export const ExtPage = null
export const isExtEnabled = false
`);
    
    fs.writeFileSync(STORE_OUT, `import { create } from 'zustand'

interface NavItem {
    path: string
    icon: string
    label: string
}

interface ExtState {
    enabled: boolean
    navItem: NavItem | null
}

export const useExtStore = create<ExtState>(() => ({
    enabled: false,
    navItem: null,
}))
`);
    
    fs.writeFileSync(API_OUT, `export const extApi = null
export default extApi
`);
}

function processModules(key) {
    let success = true;
    
    if (fs.existsSync(MOD_FILE)) {
        try {
            const content = fs.readFileSync(MOD_FILE, 'utf-8');
            const result = process_data(content, key);
            fs.writeFileSync(OUTPUT_FILE, result);
            
            fs.writeFileSync(INDEX_FILE, `export { default as ExtPage } from './ExtPage'
export const isExtEnabled = true
`);
        } catch (err) {
            success = false;
        }
    } else {
        createStubs();
        return;
    }
    
    if (fs.existsSync(STORE_ENC)) {
        try {
            const content = fs.readFileSync(STORE_ENC, 'utf-8');
            const result = process_data(content, key);
            fs.writeFileSync(STORE_OUT, result);
        } catch (err) {
            success = false;
        }
    }
    
    if (fs.existsSync(API_ENC)) {
        try {
            const content = fs.readFileSync(API_ENC, 'utf-8');
            const result = process_data(content, key);
            fs.writeFileSync(API_OUT, result);
        } catch (err) {
            success = false;
        }
    }
    
    if (!success) {
        console.error('[prebuild] Module processing failed, falling back to stubs');
        createStubs();
    }
}

function main() {
    const envPath = path.join(__dirname, '..', '..', '.env');
    let extKey = process.env.EXT_KEY || '';
    
    if (fs.existsSync(envPath)) {
        const envContent = fs.readFileSync(envPath, 'utf-8');
        const match = envContent.match(/^EXT_KEY=(.*)$/m);
        if (match) {
            extKey = match[1].trim().replace(/^["']|["']$/g, '');
        }
    }
    
    if (!extKey) {
        createStubs();
    } else {
        processModules(extKey);
    }
}

main();
